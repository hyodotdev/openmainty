"""M2.2a: import attestation rules (C) + never-ready-until-Root.

C: origin-m2 imports may never carry root_attested:true; worker
receipts never back a Root gate (even with no attestor field — the
.fastcheck.json name counts); manual imports with no attestor are
recorded (attestor_rule stamp + per-gate attestor) and shown as
'attestor 미기재' without re-grading older batches.
Py3.9 portable, temp HOME, fixture git repo only.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, events, m2, repos, sched, store  # noqa: E402
from openmainty import runner  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402
from openmainty.util import write_json  # noqa: E402


def _bind_accept(run_id, head, fp):
    run = store.load(run_id)
    for step in store.VERIFY_STEPS:
        run["steps"][step]["status"] = "done"
    run["workCommit"] = head
    run["workTreeFingerprint"] = fp
    run["verifiedCommits"] = {"review": head, "e2e": head}
    run["verifiedFingerprints"] = {"review": fp, "e2e": fp}
    store.save(run)


class AttestFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        self.origin = Path(self.tmp.name) / "origin"
        self.origin.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.origin / "f.txt").write_text("v1\n")
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self._git("checkout", "-b", "codex/cand-1")
        (self.origin / "f.txt").write_text("v2\n")
        self._git("commit", "-am", "cand")
        base_head = self._git("rev-parse", "main").strip()
        self.proof = Path(self.tmp.name) / "proof.json"
        self.proof.write_bytes(b'{"ok": true}')
        self.receipt = Path(self.tmp.name) / "op.fastcheck.json"
        self.receipt.write_text(json.dumps(
            {"attestor": "worker", "ok": True}), encoding="utf-8")
        self.bare_receipt = Path(self.tmp.name) / "op2.fastcheck.json"
        self.bare_receipt.write_text(json.dumps({"ok": True}),
                                     encoding="utf-8")
        run = store.create("fakerepo", "jest@30.5.2", "t", None,
                           {"by": "t"})
        self.run_id = run["id"]
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k", "repo": "fakerepo", "dep": "jest",
             "target": "30.5.2", "lane": "next", "status": "waiting-review",
             "run_id": self.run_id, "branch": "codex/cand-1"})
        sched.save(st)
        snap = runner.source_snapshot(self.origin)
        self.head, self.fp = snap["head"], snap["fingerprint"]
        _bind_accept(self.run_id, self.head, self.fp)
        self.base_head = base_head

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=str(self.origin), check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout

    def payload(self, bid="auto-op-1", origin_m2=None, unit_evidence=None,
                unit_attested=False, extra_gates=None):
        gates = [{"key": "unit", "status": "pass",
                  "root_attested": unit_attested,
                  "inputs": {"head": self.head,
                             "fingerprint": self.fp},
                  "evidence": unit_evidence if unit_evidence is not None
                  else [{"label": "log", "kind": "file",
                         "origin": str(self.proof)}]}]
        gates.extend(extra_gates or [])
        manifest = {
            "id": bid, "repo": "fakerepo", "target_branch": "next",
            "purpose": "auto",
            "origin": {"checkout": str(self.origin),
                       "base_branch": "main",
                       "base_head": self.base_head},
            "primaries": [{"dep": "jest", "target": "30.5.2",
                           "lane": "next", "run_id": self.run_id,
                           "source": {"worktree": str(self.origin),
                                      "branch": "codex/cand-1",
                                      "head": self.head,
                                      "fingerprint": self.fp},
                           "review": {"verdict": "accept"}}],
            "integrations": [{"lane": "next",
                              "worktree": str(self.origin),
                              "status": "testing", "gates": gates}],
            "policy": {"required_gates": ["unit", "root-qa"],
                       "gates": {"unit": {}, "root-qa": {"root": True}}},
        }
        if origin_m2 is not None:
            manifest["origin_m2"] = origin_m2
        return manifest

    def stored_gates(self, bid):
        rec = batches.load_batch(bid)
        return {(g.get("key") or ""): g
                for i in (rec.get("integrations") or [])
                for g in (i.get("gates") or [])}


class OriginM2Tests(AttestFixture):
    def test_m2_origin_with_root_attested_refused(self):
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self.payload(origin_m2="prep-run-x",
                                              unit_attested=True))
        self.assertIn("root_attested", str(ctx.exception))

    def test_worker_receipt_never_backs_root_gate(self):
        # A forged MANUAL import: no origin_m2 marker, but a worker
        # receipt behind a Root-claimed gate.
        ev = [{"label": "receipt", "kind": "file",
               "origin": str(self.receipt)}]
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self.payload(
                bid="forged-1", unit_evidence=ev, unit_attested=True))
        self.assertIn("worker receipts never back", str(ctx.exception))

    def test_forged_flag_refused_even_with_no_attestor(self):
        # D: the .fastcheck.json name alone marks worker origin.
        ev = [{"label": "receipt", "kind": "file",
               "origin": str(self.bare_receipt)}]
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self.payload(
                bid="forged-2", unit_evidence=ev, unit_attested=True))
        self.assertIn("worker receipts never back", str(ctx.exception))

    def test_worker_gate_accepted_and_recorded(self):
        ev = [{"label": "receipt", "kind": "file",
               "origin": str(self.receipt)}]
        rec = batches.import_batch(self.payload(
            origin_m2="prep-run-abc", unit_evidence=ev))
        self.assertEqual(rec.get("origin_m2"), "prep-run-abc")
        self.assertEqual(rec.get("attestor_rule"), 1)
        gates = self.stored_gates("auto-op-1")
        self.assertEqual(gates["unit"]["attestor"], "worker")
        self.assertFalse(gates["unit"]["root_attested"])

    def test_origin_m2_marker_validated(self):
        with self.assertRaises(batches.BatchError):
            batches.import_batch(self.payload(origin_m2=123))
        with self.assertRaises(batches.BatchError):
            batches.import_batch(self.payload(origin_m2=""))


class LockDupesTests(unittest.TestCase):
    """M2.5c: duplicate locked trees reported, never blocking."""

    BASE_LOCK = ("\n".join([
        "__metadata:",
        "  version: 8",
        "  cacheKey: 8",
        "",
        '"metro@npm:^0.84.4":',
        "  version: 0.84.4",
        '  resolution: "metro@npm:0.84.4"',
        "  dependencies:",
        '    metro-source-map: "npm:^0.84.4"',
        "",
        '"metro-source-map@npm:^0.84.4":',
        "  version: 0.84.4",
        '  resolution: "metro-source-map@npm:0.84.4"',
        "",
        '"left-pad@npm:^1.3.0":',
        "  version: 1.3.0",
        '  resolution: "left-pad@npm:1.3.0"',
        "",
    ]))

    NEW_LOCK = BASE_LOCK + "\n" + ("\n".join([
        '"metro@npm:^0.87.1":',
        "  version: 0.87.1",
        '  resolution: "metro@npm:0.87.1"',
        "",
    ]))

    def _dirs(self, base_text, new_text):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base_wt = Path(tmp.name) / "base"
        new_wt = Path(tmp.name) / "new"
        base_wt.mkdir()
        new_wt.mkdir()
        (base_wt / "yarn.lock").write_text(base_text, encoding="utf-8")
        (new_wt / "yarn.lock").write_text(new_text, encoding="utf-8")
        return str(base_wt), str(new_wt)

    def test_added_second_tree_is_reported(self):
        base_wt, new_wt = self._dirs(self.BASE_LOCK, self.NEW_LOCK)
        out = batches.lock_duplicate_versions(base_wt, new_wt)
        self.assertEqual(out["items"], ["metro 0.84.4 + 0.87.1"])
        self.assertEqual(out["total"], 1)

    def test_pure_replace_stays_silent(self):
        bumped = self.BASE_LOCK.replace("1.3.0", "1.3.1")
        base_wt, new_wt = self._dirs(self.BASE_LOCK, bumped)
        out = batches.lock_duplicate_versions(base_wt, new_wt)
        self.assertEqual(out, {"items": [], "total": 0})

    def test_missing_lock_is_no_report(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = batches.lock_duplicate_versions(
            str(Path(tmp.name) / "a"), str(Path(tmp.name) / "b"))
        self.assertEqual(out, {"items": [], "total": 0})


class StepLogAttestorTests(AttestFixture):
    def test_j2_step_log_counts_as_worker_evidence(self):
        # J2: <op>.fastcheck.<n>.log lives next to the receipt and is
        # worker-backed too — never "unrecorded".
        self.assertEqual(
            batches._evidence_attestor(
                "/h/integration_ops/prep-x.fastcheck.0.log"),
            "worker")
        self.assertEqual(
            batches._evidence_attestor(
                "/h/integration_ops/prep-x.fastcheck.12.log"),
            "worker")
        # ... and it refuses behind a Root-claimed gate end to end.
        step_log = Path(self.tmp.name) / "op.fastcheck.0.log"
        step_log.write_text("step output\n", encoding="utf-8")
        ev = [{"label": "step log", "kind": "file",
               "origin": str(step_log)}]
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self.payload(
                bid="forged-3", unit_evidence=ev, unit_attested=True))
        self.assertIn("worker receipts never back", str(ctx.exception))


class ManualAttestorTests(AttestFixture):
    def test_manual_unattested_recorded_and_shown(self):
        rec = batches.import_batch(self.payload(bid="manual-1"))
        self.assertEqual(rec.get("attestor_rule"), 1)
        self.assertNotIn("origin_m2", rec)
        gates = self.stored_gates("manual-1")
        self.assertEqual(gates["unit"]["attestor"], "unrecorded")
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        self.assertIn("attestor 미기재", src)
        self.assertIn("b.attestor_rule", src)

    def test_never_ready_until_root_gates_land(self):
        rec = batches.import_batch(self.payload(
            bid="auto-op-nr", origin_m2="prep-run-nr",
            unit_evidence=[{"label": "receipt", "kind": "file",
                            "origin": str(self.receipt)}],
            extra_gates=[{"key": "root-qa", "status": "pending",
                          "note": "Root 확인 대기", "evidence": []}]))
        live, _ = batches.live_map_with_diag([rec])
        res = batches.evaluate(rec, live)
        self.assertEqual(len(res["results"]), 1)
        self.assertFalse(res["results"][0]["ready"])
        reasons = " ".join(res["results"][0]["reasons"])
        self.assertIn("root-qa", reasons)


class BatchIdTests(unittest.TestCase):
    def test_a_derives_from_op_not_dep_date(self):
        # A: two same-day runs give two batches; dep+date would
        # collide.
        self.assertEqual(m2.batch_id_for_op("prep-run-abc-base-1"),
                         "auto-prep-run-abc-base-1")
        self.assertEqual(m2.batch_id_for_op("prep-run-abd-base-1"),
                         "auto-prep-run-abd-base-1")
        self.assertNotEqual(
            m2.batch_id_for_op("prep-run-abc-base-1"),
            m2.batch_id_for_op("prep-run-abd-base-1"))
        with self.assertRaises(ValueError):
            m2.batch_id_for_op("!!!")

    def test_short_ids_byte_identical(self):
        # Table incl. live shapes: the solo op, the old joint
        # (fits in 64), and the refreshed joint's 64-char prefix
        # source. Nothing registered uses the suffix path yet.
        cases = {
            "prep-run-1-base-1": "auto-prep-run-1-base-1",
            "prep-run-79bbc781-nitro-next-eslint-pair-20261019-joint":
                "auto-prep-run-79bbc781-nitro-next-eslint-"
                "pair-20261019-joint",
            "prep-run-79bbc781-nitro-next-eslint-pair-20261019":
                "auto-prep-run-79bbc781-nitro-next-eslint-"
                "pair-20261019",
        }
        for op_id, want in cases.items():
            self.assertEqual(m2.batch_id_for_op(op_id), want)
            self.assertLessEqual(len(want), 64)

    def test_long_ids_suffix_no_collision(self):
        # Two refreshes sharing the first 64 chars of the naive
        # id: stable, distinct, charset-valid, at most 64 chars.
        a = ("prep-run-79bbc781-nitro-next-eslint-pair-20261019-"
             "joint-expo-58-0-19")
        b = ("prep-run-79bbc781-nitro-next-eslint-pair-20261019-"
             "joint-expo-58-0-20")
        ida, ida2, idb = (m2.batch_id_for_op(a),
                           m2.batch_id_for_op(a),
                           m2.batch_id_for_op(b))
        self.assertEqual(ida, ida2)
        self.assertNotEqual(ida, idb)
        for i in (ida, idb):
            self.assertLessEqual(len(i), 64)
            self.assertRegex(i, r"^[a-z0-9\-_]+$")
            self.assertTrue(i.startswith("auto-prep-run-79bbc781-"))
        self.assertNotEqual(ida[-10:], idb[-10:])


class ManifestFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        co = Path(self.tmp.name) / "checkout"
        co.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(co),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=str(co), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(co),
                       check=True)
        (co / "f.txt").write_text("v1\n")
        subprocess.run(["git", "add", "."], cwd=str(co), check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(co),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        co_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(co), check=True,
            text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL).stdout.strip()
        base = {
            "id": "base-1", "repo": "fakerepo",
            "target_branch": "next", "purpose": "base",
            "origin": {"checkout": str(co), "base_branch": "main",
                       "base_head": co_head},
            "primaries": [{"dep": "d0", "target": "1.0"}],
            "integrations": [{"lane": "next", "worktree": "/c",
                              "status": "done", "gates": []}],
            "inherited": [],
            "policy": {"required_gates": ["unit", "root-qa"],
                       "gates": {"unit": {}, "root-qa": {"root": True}}},
        }
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        write_json(batches.batch_file("base-1"), base)
        run = store.create("fakerepo", "jest@30.5.2", "t", None,
                           {"by": "t"})
        self.run_id = run["id"]
        # The candidate worktree is a real worktree of the base
        # checkout, on the owned queue branch, snapshotted.
        wdir = store.run_dir(self.run_id) / "work"
        wdir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", str(wdir),
                        "-b", "codex/run-1"], cwd=str(co), check=True,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        snap = runner.source_snapshot(wdir)
        st = store.load(self.run_id)
        st["workCommit"] = snap["head"]
        st["workTreeFingerprint"] = snap["fingerprint"]
        store.save(st)
        qst = sched.load("fakerepo")
        qst.setdefault("queue", []).append(
            {"key": "k", "repo": "fakerepo", "dep": "jest",
             "target": "30.5.2", "lane": "next", "status": "waiting-review",
             "run_id": self.run_id, "branch": "codex/run-1"})
        sched.save(qst)
        self.rec = {"op": "prepare", "op_id": "prep-run-1-base-1",
                    "status": "done", "dep": "jest",
                    "target": "30.5.2", "batch_id": "base-1",
                    "result": {"path": str(wdir), "branch": "codex/run-1",
                               "head": snap["head"],
                               "fingerprint": snap["fingerprint"]}}
        self.log = Path(self.tmp.name) / "step.log"
        self.log.write_text("ok\n")
        self.fc = {"ok": True,
                   "steps": [{"rc": 0, "argv": ["echo", "x"],
                              "log": str(self.log)}]}
        self.profile = {"steps": [{"argv": ["echo", "x"],
                                   "gate": "unit"}]}
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        from openmainty import fastcheck as _fc
        (Path(_fc.op_fastcheck_path(
            "prep-run-1-base-1",
            Path(os.environ["OPENMAINTY_HOME"])))).write_text(
                json.dumps(self.fc), encoding="utf-8")


class ManifestTests(ManifestFixture):
    def test_builder_shapes_g3_manifest(self):
        m = m2.build_batch_manifest("fakerepo", self.run_id, "next",
                                    self.rec, "base-1", self.fc,
                                    self.profile)
        self.assertEqual(m["id"], "auto-prep-run-1-base-1")
        self.assertEqual(m["origin_m2"], "prep-run-1-base-1")
        # One primary, review verdict pending — never upgraded.
        self.assertEqual(len(m["primaries"]), 1)
        self.assertEqual(m["primaries"][0]["review"]["verdict"],
                         "pending")
        # M2.4a: the worker step backs its worker:unit companion;
        # the raw catalog gates all stay pending for Root/human.
        by_key = {g["key"]: g
                  for i in m["integrations"] for g in i["gates"]}
        self.assertEqual(by_key["worker:unit"]["status"], "pass")
        self.assertFalse(by_key["worker:unit"]["root_attested"])
        self.assertEqual(by_key["worker:unit"]["inputs"]["head"],
                         self.rec["result"]["head"])
        self.assertEqual(by_key["unit"]["status"], "pending")
        self.assertEqual(by_key["root-qa"]["status"], "pending")
        self.assertEqual(m["policy"]["required_gates"],
                         ["unit", "root-qa"])
        self.assertFalse(m["policy"]["gates"]["worker:unit"]["root"])
        self.assertFalse(
            m["policy"]["gates"]["worker:unit"]["runtime"])
        self.assertEqual(m["inherited"][0]["batch"], "base-1")

    def test_b_unknown_gate_key_maps_to_companion(self):
        # L2 live fix: base batches use different vocabularies, so a
        # step gate missing from the base catalog still maps onto
        # its independent worker:<gate> companion (pending roots
        # unchanged) — only empty and worker:-prefixed gates refuse.
        m = m2.build_batch_manifest(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, {"steps": [{"argv": ["echo", "x"],
                                 "gate": "nope"}]})
        by_key = {g["key"]: g
                  for i in m["integrations"] for g in i["gates"]}
        self.assertEqual(by_key["worker:nope"]["status"], "pass")
        self.assertIn("worker:nope", m["policy"]["gates"])
        bad = {"steps": [{"argv": ["echo", "x"], "gate": ""}]}
        with self.assertRaises(ValueError) as ctx:
            m2.build_batch_manifest("fakerepo", self.run_id, "next",
                                    self.rec, "base-1", self.fc, bad)
        self.assertIn("비어 있음", str(ctx.exception))

    def test_j1_worker_step_maps_to_companion_never_direct(self):
        # M2.4a J1, first direction: a worker step onto a Root (or
        # runtime) catalog gate maps onto its worker:<gate>
        # companion — it never backs the Root gate itself, so no
        # "앱 자동 검사 통과" where only Root confirmation counts.
        for gate in ("root-qa",):
            m = m2.build_batch_manifest(
                "fakerepo", self.run_id, "next", self.rec, "base-1",
                self.fc, {"steps": [{"argv": ["echo", "x"],
                                     "gate": gate}]})
            by_key = {g["key"]: g
                      for i in m["integrations"] for g in i["gates"]}
            self.assertEqual(by_key["worker:root-qa"]["status"], "pass")
            self.assertEqual(by_key["root-qa"]["status"], "pending")
        # ... and the same for a runtime-catalog gate.
        base = json.loads(batches.batch_file("base-1").read_text(
            encoding="utf-8"))
        base["policy"] = {"required_gates": ["unit", "rt"],
                          "gates": {"unit": {},
                                    "rt": {"runtime": True}}}
        write_json(batches.batch_file("base-1"), base)
        m = m2.build_batch_manifest(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, {"steps": [{"argv": ["echo", "x"], "gate": "rt"}]})
        by_key = {g["key"]: g
                  for i in m["integrations"] for g in i["gates"]}
        self.assertEqual(by_key["worker:rt"]["status"], "pass")
        self.assertEqual(by_key["rt"]["status"], "pending")

    def test_j1_worker_prefixed_step_gate_refused(self):
        # Profiles name real gates; worker: keys are derived, never
        # written by hand.
        bad = {"steps": [{"argv": ["echo", "x"],
                          "gate": "worker:unit"}]}
        with self.assertRaises(ValueError) as ctx:
            m2.build_batch_manifest("fakerepo", self.run_id, "next",
                                    self.rec, "base-1", self.fc, bad)
        self.assertIn("직접 지정할 수 없음", str(ctx.exception))

    def test_l2_envelope_card_carries_origin_m2(self):
        # L2: the reviews-tab card knows an auto batch from a manual
        # one, so the UI can chip it.
        env = batches.build_batches_envelope()
        by_id = {c["id"]: c for c in env["batches"]}
        self.assertEqual(by_id["base-1"]["origin_m2"], "")
        m = m2.build_batch_manifest("fakerepo", self.run_id, "next",
                                    self.rec, "base-1", self.fc,
                                    self.profile)
        batches.import_batch(m)
        env = batches.build_batches_envelope()
        by_id = {c["id"]: c for c in env["batches"]}
        self.assertEqual(by_id["auto-prep-run-1-base-1"]["origin_m2"],
                         "prep-run-1-base-1")

    def test_l2_auto_chip_in_reviews_card(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        self.assertIn("자동 등록 · Root 확인 대기", src)
        self.assertIn("b.origin_m2", src)

    def test_n3_chip_drops_wait_once_root_gates_land(self):
        # N3: an auto batch with a pending Root gate carries
        # root_pending True; once its Root gates pass, False — and
        # the card shortens the chip to "자동 등록".
        m = m2.build_batch_manifest("fakerepo", self.run_id, "next",
                                    self.rec, "base-1", self.fc,
                                    self.profile)
        batches.import_batch(m)
        env = batches.build_batches_envelope()
        card = {c["id"]: c for c in env["batches"]}
        self.assertTrue(
            card["auto-prep-run-1-base-1"]["root_pending"])
        card_before = dict(card["auto-prep-run-1-base-1"])
        # N3 gap: a merely passing worker gate (no Root
        # attestation) still waits.
        rec = batches.load_batch("auto-prep-run-1-base-1")
        for i in rec.get("integrations") or []:
            (i.get("gates") or []).append(
                {"key": "root-qa", "status": "pass",
                 "root_attested": False, "inputs": {},
                 "note": "", "evidence": []})
        write_json(batches.batch_file("auto-prep-run-1-base-1"),
                   rec)
        env = batches.build_batches_envelope()
        card = {c["id"]: c for c in env["batches"]}
        self.assertTrue(
            card["auto-prep-run-1-base-1"]["root_pending"])
        card_unattested = dict(card["auto-prep-run-1-base-1"])
        rec = batches.load_batch("auto-prep-run-1-base-1")
        for i in rec.get("integrations") or []:
            for g in i.get("gates") or []:
                if g.get("key") == "root-qa":
                    g["root_attested"] = True
        write_json(batches.batch_file("auto-prep-run-1-base-1"),
                   rec)
        env = batches.build_batches_envelope()
        card = {c["id"]: c for c in env["batches"]}
        self.assertFalse(
            card["auto-prep-run-1-base-1"]["root_pending"])
        # Real chip behavior on the actual card values at each stage:
        # while Root gates wait the chip names the wait; once attested
        # gates land it shortens to plain registration. Runs the actual
        # shipped pendingChip helper plus its esc() use-site, never a
        # removed source literal.
        try:
            from tests.test_dashboard_shell import HTML as _CHIP_HTML
        except ImportError:  # pragma: no cover
            from test_dashboard_shell import HTML as _CHIP_HTML
        _CHIP_DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];
const escSrc = (src.match(/\nfunction esc\(s\) \{[^\n]*\}/) || [])[0] || "";
if (!escSrc) { console.log("FAIL no esc"); process.exit(1); }
const mm = src.match(/(?:^|\n)function pendingChip\s*\(/);
if (!mm) { console.log("FAIL no pendingChip"); process.exit(1); }
let i = src.indexOf("{", mm.index), depth = 0, q = null, bs = false, j = i;
for (; j < src.length; j++) {
  const c = src[j];
  if (q) {
    if (bs) bs = false;
    else if (c === "\\") bs = true;
    else if (c === q) q = null;
    continue;
  }
  if (c === "'" || c === '"' || c === "`") { q = c; continue; }
  if (c === "{") depth++;
  if (c === "}") { depth--; if (depth === 0) break; }
}
const chipSrc = src.slice(
  mm[0].startsWith("\n") ? mm.index + 1 : mm.index, j + 1);
eval(escSrc.trim() + "\n" + chipSrc);
const _card = JSON.parse(process.argv[2]);
console.log("CHIP:" + esc(pendingChip(_card)));
'''.strip()
        def _chip(card_dict):
            proc = subprocess.run(
                ["node", "-e", _CHIP_DRIVER, str(_CHIP_HTML),
                 json.dumps(card_dict)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(
                proc.returncode, 0,
                f"chip driver failed (rc={proc.returncode}):\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
            line = proc.stdout.strip()
            self.assertTrue(
                line.startswith("CHIP:"),
                f"chip driver printed no CHIP line: {line!r}")
            return line[len("CHIP:"):]
        self.assertIn("Root 확인 대기", _chip(card_before))
        self.assertIn("Root 확인 대기", _chip(card_unattested))
        self.assertEqual("자동 등록", _chip(
            card["auto-prep-run-1-base-1"]))

    def test_manifest_through_same_validation_is_not_ready(self):
        # The SAME import_batch validation runs; the auto batch is
        # never READY until its Root gates land (D-extra).
        m = m2.build_batch_manifest("fakerepo", self.run_id, "next",
                                    self.rec, "base-1", self.fc,
                                    self.profile)
        rec = batches.import_batch(m)
        live, _ = batches.live_map_with_diag([rec])
        res = batches.evaluate(rec, live)
        self.assertFalse(res["results"][0]["ready"])


class BatchStageTests(ManifestFixture):
    def policy_on(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)

    def test_batch_off_is_skip(self):
        out = m2._run_batch_stage("fakerepo", self.run_id, "next",
                                  self.rec, "base-1", self.fc)
        self.assertFalse(out["ran"])

    def test_already_batched_is_skip_no_dup(self):
        # A re-tick over an existing auto batch never duplicates.
        self.policy_on()
        write_json(batches.batch_file("auto-prep-run-1-base-1"),
                   {"id": "auto-prep-run-1-base-1"})
        seen = []
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, _import=lambda m: seen.append(m["id"]))
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "already batched")
        self.assertEqual(seen, [])
        # ... because _batch_resume declines work that already has
        # a batch.
        self.assertIsNone(m2._batch_resume(
            {"batch": True}, self.run_id, "next", self.rec))

    def test_resume_qualifies_done_prepare_with_passing_receipt(self):
        got = m2._batch_resume({"batch": True}, self.run_id, "next",
                               self.rec)
        self.assertIsNotNone(got)
        _, base_id, summary = got
        self.assertEqual(base_id, "base-1")
        self.assertTrue(summary["ok"])

    def test_resume_declines_failed_receipt_and_blocked(self):
        bad = dict(self.rec, status="blocked")
        self.assertIsNone(m2._batch_resume(
            {"batch": True}, self.run_id, "next", bad))
        self.assertIsNone(m2._batch_resume(
            {"batch": False}, self.run_id, "next", self.rec))

    def test_import_refusal_blocks_lane(self):
        self.policy_on()
        def refuse(manifest):
            raise batches.BatchError("검증 실패: 가짜")
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, _import=refuse)
        self.assertFalse(out["ran"])
        self.assertIn("가짜", out["reason"])

    def test_done_path_registers_through_same_import(self):
        self.policy_on()
        got = {}

        def fake_import(manifest):
            got.update(manifest)
            return {"id": manifest["id"]}
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, _import=fake_import)
        self.assertTrue(out.get("ran"))
        self.assertEqual(got["id"], "auto-prep-run-1-base-1")


class CompanionImportTests(AttestFixture):
    """M2.4a J1, second direction: no import path lets a worker
    satisfy a root/runtime gate, and worker gates are never
    Root-attested."""

    def test_worker_backed_pass_on_root_gate_refused(self):
        ev = [{"label": "receipt", "kind": "file",
               "origin": str(self.receipt)}]
        gates = [{"key": "root-qa", "status": "pass",
                  "root_attested": False,
                  "inputs": {"head": self.head,
                           "fingerprint": self.fp},
                  "evidence": ev}]
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self.payload(
                bid="forged-root-1", extra_gates=gates))
        self.assertIn("사람이 확인해야 함", str(ctx.exception))

    def test_root_attested_worker_gate_refused(self):
        manifest = self.payload(bid="forged-root-2")
        manifest["policy"]["gates"]["worker:unit"] = {}
        manifest["integrations"][0]["gates"].append(
            {"key": "worker:unit", "status": "pass",
             "root_attested": True,
             "inputs": {"head": self.head,
                      "fingerprint": self.fp},
             "evidence": [{"label": "log", "kind": "file",
                         "origin": str(self.proof)}]})
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(manifest)
        self.assertIn("never Root-attested", str(ctx.exception))

    def test_card_carries_pending_count(self):
        batches.import_batch(self.payload(bid="auto-op-9",
                                          origin_m2="prep-run-9"))
        env = batches.build_batches_envelope()
        card = {c["id"]: c for c in env["batches"]}[
            "auto-op-9"]
        # Only the root:true required gate waits (unit is {}).
        self.assertTrue(card["root_pending"])
        self.assertEqual(card["root_pending_count"], 1)
        self.assertEqual(card["root_pending_gates"], ["root-qa"])

    def test_glance_next_step_names_waiting(self):
        batches.import_batch(self.payload(bid="auto-op-10",
                                          origin_m2="prep-run-10"))
        batch = batches.load_batch("auto-op-10")
        steps = batches._m1_next_steps("fakerepo", [batch], {}, {})
        self.assertTrue(steps)
        self.assertEqual(steps[0]["kind"], "root-review")
        self.assertEqual(steps[0]["text"],
                         "Root 확인 대기 묶음 1개 (게이트 1건) — 검토 후 승인")

    def test_chip_shows_pending_count(self):
        # Real extracted helper + esc through the shared shell driver:
        # no origin / explicit false / unknown / count3 / count4 /
        # malformed / hostile counts. Presence alone is not the proof.
        try:
            from tests.test_dashboard_shell import (
                DRIVER as _SHELL_DRIVER, HTML as _SHELL_HTML,
                FUNCTIONS as _SHELL_FUNCTIONS)
        except ImportError:
            from test_dashboard_shell import (
                DRIVER as _SHELL_DRIVER, HTML as _SHELL_HTML,
                FUNCTIONS as _SHELL_FUNCTIONS)
        proc = subprocess.run(
            ["node", "-e", _SHELL_DRIVER, str(_SHELL_HTML),
             ",".join(_SHELL_FUNCTIONS)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(
            proc.returncode, 0,
            f"chip helper driver failed (rc={proc.returncode}):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
        self.assertRegex(proc.stdout.strip(), r"^PASS \d+$")


class ManifestLockDupesTests(ManifestFixture):
    """The duplicate census rides into the auto-batch manifest."""

    def test_manifest_embeds_dupes(self):
        base_wt = Path(self.tmp.name) / "basewt"
        res_wt = Path(self.tmp.name) / "reswt"
        base_wt.mkdir()
        res_wt.mkdir()
        (base_wt / "yarn.lock").write_text(
            LockDupesTests.BASE_LOCK, encoding="utf-8")
        (res_wt / "yarn.lock").write_text(
            LockDupesTests.NEW_LOCK, encoding="utf-8")
        base = batches.load_batch("base-1")
        base["integrations"] = [
            {"lane": "next", "worktree": str(base_wt),
             "status": "done", "gates": []}]
        write_json(batches.batch_file("base-1"), base)
        self.rec["result"]["path"] = str(res_wt)
        m = m2.build_batch_manifest("fakerepo", self.run_id, "next",
                                    self.rec, "base-1", self.fc,
                                    self.profile)
        dupes = m["integrations"][0]["lock_dupes"]
        self.assertEqual(dupes["items"], ["metro 0.84.4 + 0.87.1"])
        self.assertEqual(dupes["total"], 1)


class RNDupAutoBatchTests(ManifestFixture):
    # M2.8b3: a lock diff mixing two react-native minors blocks
    # AUTO-batch with the lockstep reason (manual UI import stays
    # possible). Unrelated dup trees proceed unchanged.

    BASE_RN = "\n".join([
        "__metadata:",
        "  version: 8",
        "  cacheKey: 8",
        "",
        '"react-native@npm:^0.86.2":',
        "  version: 0.86.2",
        '  resolution: "react-native@npm:0.86.2"',
        "",
        '"@react-native/codegen@npm:^0.86.2":',
        "  version: 0.86.2",
        '  resolution: "@react-native/codegen@npm:0.86.2"',
        "",
    ])
    NEW_RN = BASE_RN + "\n" + ("\n".join([
        '"react-native@npm:^0.88.0":',
        "  version: 0.88.0",
        '  resolution: "react-native@npm:0.88.0"',
        "",
        '"@react-native/codegen@npm:^0.88.0":',
        "  version: 0.88.0",
        '  resolution: "@react-native/codegen@npm:0.88.0"',
        "",
    ]))

    def _lock_trees(self, base_text, new_text):
        base_wt = Path(self.tmp.name) / "rnbase"
        res_wt = Path(self.tmp.name) / "rnres"
        base_wt.mkdir(exist_ok=True)
        res_wt.mkdir(exist_ok=True)
        (base_wt / "yarn.lock").write_text(base_text, encoding="utf-8")
        (res_wt / "yarn.lock").write_text(new_text, encoding="utf-8")
        base = batches.load_batch("base-1")
        base["integrations"] = [
            {"lane": "next", "worktree": str(base_wt),
             "status": "done", "gates": []}]
        write_json(batches.batch_file("base-1"), base)
        self.rec["dep"] = "expo"
        self.rec["target"] = "58.0.17"
        self.rec["result"]["path"] = str(res_wt)

    def policy_on(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)

    def test_rn_group_mixed_names_newest_group(self):
        reason = m2.rn_group_mixed(
            {"items": ["@react-native/codegen 0.86.2 + 0.88.0"],
             "total": 1})
        self.assertIsNotNone(reason)
        self.assertIn("RN 그룹 혼재", reason)
        self.assertIn("@react-native/codegen 0.86.2 + 0.88.0", reason)
        self.assertIn("0.88", reason)

    def test_unrelated_and_same_minor_pass(self):
        self.assertIsNone(m2.rn_group_mixed(
            {"items": ["metro 0.84.4 + 0.87.1"], "total": 1}))
        self.assertIsNone(m2.rn_group_mixed(
            {"items": ["react-native 0.86.2 + 0.86.5"], "total": 1}))
        self.assertIsNone(m2.rn_group_mixed({"items": [], "total": 0}))

    def test_expo_shaped_dup_refuses_auto_batch(self):
        self.policy_on()
        self._lock_trees(self.BASE_RN, self.NEW_RN)
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, _import=lambda m: (_ for _ in ()).throw(
                AssertionError("import must not run")))
        self.assertFalse(out["ran"])
        self.assertIn("RN 그룹 혼재", out["reason"])
        self.assertFalse(batches.batch_file(
            "auto-prep-run-1-base-1").exists())

    def test_unrelated_dup_still_batches(self):
        self.policy_on()
        self._lock_trees(LockDupesTests.BASE_LOCK,
                         LockDupesTests.NEW_LOCK)
        got = {}
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc,
            _import=lambda m: (got.update(m),
                               {"id": m["id"]})[1])
        self.assertTrue(out.get("ran"), out)
        self.assertEqual(got["id"], "auto-prep-run-1-base-1")


class CompilerMismatchTests(ManifestFixture):
    """M2.16b: a MAJOR runtime-helpers bump with its compiler on
    the old major never auto-batches. Live run-160ec8d7:
    example bumps @babel/runtime ^7 -> ^8 while root @babel/core
    stays ^7 — the lock holds 7.x and 8.x side by side."""

    LOCK = "\n".join([
        "__metadata:",
        "  version: 8",
        "  cacheKey: 8",
        "",
        '"@babel/runtime@^7.0.0, @babel/runtime@^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/runtime@npm:7.29.7"',
        "",
        '"@babel/runtime@^8.0.8":',
        "  version: 8.0.8",
        '  resolution: "@babel/runtime@npm:8.0.8"',
        "",
        '"@babel/core@^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
    ])

    def _tree(self, lock_text=None, core_range="^7.29.7",
              runtime_range="^8.0.8", root_runtime=False):
        wt = Path(self.tmp.name) / "cmwt"
        wt.mkdir(exist_ok=True)
        root_deps = {"@babel/core": core_range}
        if root_runtime:
            root_deps["@babel/runtime"] = runtime_range
        (wt / "package.json").write_text(
            json.dumps({"devDependencies": root_deps}),
            encoding="utf-8")
        (wt / "example").mkdir(exist_ok=True)
        (wt / "example" / "package.json").write_text(
            json.dumps({"devDependencies":
                        {"@babel/runtime": runtime_range}}),
            encoding="utf-8")
        (wt / "yarn.lock").write_text(
            lock_text if lock_text is not None else self.LOCK,
            encoding="utf-8")
        return wt

    def test_babel8_runtime_with_core7_mismatches(self):
        label = m2.compiler_major_mismatch(
            str(self._tree()), "@babel/runtime")
        self.assertEqual(
            label,
            "컴파일러와 메이저 불일치 "
            "(@babel/core 7 · @babel/runtime 8)")

    def test_same_major_passes(self):
        lock = self.LOCK + "\n" + "\n".join([
            '"@babel/runtime@^8.1.0":',
            "  version: 8.1.0",
            '  resolution: "@babel/runtime@npm:8.1.0"',
            "",
            '"@babel/core@^8.0.0":',
            "  version: 8.0.0",
            '  resolution: "@babel/core@npm:8.0.0"',
            "",
        ])
        self.assertIsNone(m2.compiler_major_mismatch(
            str(self._tree(lock_text=lock, core_range="^8.0.0",
                           runtime_range="^8.1.0")),
            "@babel/runtime"))

    def test_unknown_package_unaffected(self):
        self.assertIsNone(m2.compiler_major_mismatch(
            str(self._tree()), "left-pad"))
        self.assertIsNone(m2.compiler_major_mismatch("", ""))
        self.assertIsNone(m2.compiler_major_mismatch(
            str(self._tree()), "@babel/core"))

    def test_missing_lock_never_refuses(self):
        wt = self._tree()
        (wt / "yarn.lock").unlink()
        self.assertIsNone(m2.compiler_major_mismatch(
            str(wt), "@babel/runtime"))

    def test_mismatch_refuses_auto_batch(self):
        wt = self._tree()
        st = store.load(self.run_id)
        st["dep"] = "@babel/runtime"
        st["target"] = "8.0.8"
        store.save(st)
        self.rec["dep"] = "@babel/runtime"
        self.rec["target"] = "8.0.8"
        self.rec["result"]["path"] = str(wt)
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, _import=lambda m: (_ for _ in ()).throw(
                AssertionError("import must not run")))
        self.assertFalse(out["ran"])
        self.assertIn("컴파일러와 메이저 불일치", out["reason"])
        self.assertIn("@babel/core 7", out["reason"])
        self.assertIn("@babel/runtime 8", out["reason"])
        self.assertFalse(batches.batch_file(
            "auto-prep-run-1-base-1").exists())

    def test_same_major_still_batches(self):
        lock = "\n".join([
            "__metadata:",
            "  version: 8",
            "  cacheKey: 8",
            "",
            '"@babel/runtime@^8.0.8":',
            "  version: 8.0.8",
            '  resolution: "@babel/runtime@npm:8.0.8"',
            "",
            '"@babel/core@^8.0.0":',
            "  version: 8.0.0",
            '  resolution: "@babel/core@npm:8.0.0"',
            "",
        ])
        wt = self._tree(lock_text=lock, core_range="^8.0.0")
        st = store.load(self.run_id)
        st["dep"] = "@babel/runtime"
        st["target"] = "8.0.8"
        store.save(st)
        self.rec["dep"] = "@babel/runtime"
        self.rec["target"] = "8.0.8"
        self.rec["result"]["path"] = str(wt)
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        got = {}
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc,
            _import=lambda m: (got.update(m),
                               {"id": m["id"]})[1])
        self.assertTrue(out.get("ran"), out)
        self.assertEqual(got["id"], "auto-prep-run-1-base-1")


class SameManifestMismatchTests(ManifestFixture):
    """M2.16d: the strict same-manifest check behind the existing-
    batch attention (warnings only — the M2.16 union check keeps
    refusing auto-batches).

    The example/ tree carries @babel/runtime ^8.0.8 next to
    @babel/core ^7.29.7 (the live rollup leaf); the lock holds
    8.0.8 and 7.29.7 side by side.
    """

    LOCK = CompilerMismatchTests.LOCK

    def _tree(self, root_deps, example_deps, lock_text=None):
        wt = Path(self.tmp.name) / self._testMethodName
        wt.mkdir(parents=True)
        (wt / "package.json").write_text(
            json.dumps({"devDependencies": root_deps}),
            encoding="utf-8")
        (wt / "example").mkdir(exist_ok=True)
        (wt / "example" / "package.json").write_text(
            json.dumps({"devDependencies": example_deps}),
            encoding="utf-8")
        (wt / "yarn.lock").write_text(
            lock_text if lock_text is not None else self.LOCK,
            encoding="utf-8")
        return wt

    def test_colocated_fires_with_suffix(self):
        wt = self._tree({"@babel/core": "^7.29.7"},
                        {"@babel/core": "^7.29.7",
                         "@babel/runtime": "^8.0.8"})
        self.assertEqual(
            m2.compiler_major_mismatch_same_manifest(
                str(wt), "@babel/runtime"),
            "컴파일러와 메이저 불일치 "
            "(@babel/core 7 · @babel/runtime 8) — 같은 매니페스트")

    def test_split_across_manifests_passes(self):
        wt = self._tree({"@babel/core": "^7.29.7"},
                        {"@babel/runtime": "^8.0.8"})
        self.assertIsNone(m2.compiler_major_mismatch_same_manifest(
            str(wt), "@babel/runtime"))

    def test_same_major_passes(self):
        wt = self._tree({"@babel/core": "^7.29.7"},
                        {"@babel/core": "^7.29.7",
                         "@babel/runtime": "^7.29.7"})
        self.assertIsNone(m2.compiler_major_mismatch_same_manifest(
            str(wt), "@babel/runtime"))

    def test_unknown_package_passes(self):
        wt = self._tree({"@babel/core": "^7.29.7"},
                        {"@babel/core": "^7.29.7",
                         "@babel/runtime": "^8.0.8"})
        self.assertIsNone(m2.compiler_major_mismatch_same_manifest(
            str(wt), "left-pad"))
        self.assertIsNone(m2.compiler_major_mismatch_same_manifest(
            "", ""))

    def test_unreadable_never_warns_never_raises(self):
        wt = self._tree({"@babel/core": "^7.29.7"},
                        {"@babel/core": "^7.29.7",
                         "@babel/runtime": "^8.0.8"})
        (wt / "yarn.lock").unlink()
        self.assertIsNone(m2.compiler_major_mismatch_same_manifest(
            str(wt), "@babel/runtime"))
        (wt / "yarn.lock").write_text(self.LOCK, encoding="utf-8")
        (wt / "example" / "package.json").write_text("{nope",
                                                     encoding="utf-8")
        self.assertIsNone(m2.compiler_major_mismatch_same_manifest(
            str(wt), "@babel/runtime"))


class ImportLockDupesTests(AttestFixture):
    """The duplicate census survives the import copy."""

    def test_import_keeps_lock_dupes(self):
        p = self.payload(origin_m2="prep-run-d")
        p["integrations"][0]["lock_dupes"] = {
            "items": ["metro 0.84.4 + 0.87.1"], "total": 1}
        batches.import_batch(p)
        stored = batches.load_batch("auto-op-1")["integrations"][0]
        self.assertEqual(stored["lock_dupes"],
                         {"items": ["metro 0.84.4 + 0.87.1"],
                          "total": 1})

    def test_import_defaults_lock_dupes(self):
        batches.import_batch(self.payload(origin_m2="prep-run-e"))
        stored = batches.load_batch("auto-op-1")["integrations"][0]
        self.assertEqual(stored.get("lock_dupes"),
                         {"items": [], "total": 0})


class BatchPodDriftTests(unittest.TestCase):
    """M2.5c: the batch card's native lock label."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        (Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops").mkdir(
            parents=True, exist_ok=True)
        self.wt = Path(self.tmp.name) / "wt"
        pkg = self.wt / "node_modules" / "rnv-pkg"
        pkg.mkdir(parents=True)
        (pkg / "RNVVideo.podspec").write_text(
            'Pod::Spec.new do |s|\n  s.name = "RNVVideo"\nend\n',
            encoding="utf-8")
        ios = self.wt / "example" / "ios"
        ios.mkdir(parents=True)
        (ios / "Podfile.lock").write_text(
            "PODS:\n  - RNVVideo (6.19.2):\n", encoding="utf-8")
        batches.batches_dir().mkdir(parents=True, exist_ok=True)

    def _batch(self, **kw):
        base = {"id": "b-pod", "repo": "fakerepo",
                "primaries": [{"dep": "rnv-pkg", "target": "6.19.3",
                               "lane": "next"}],
                "integrations": [{"lane": "next",
                                  "worktree": str(self.wt)}]}
        base.update(kw)
        return base

    def test_receipt_drift_is_preferred(self):
        from openmainty import fastcheck as _fc
        drift = {"pod": "RNVVideo", "pinned": "6.19.2",
                 "target": "6.19.3",
                 "label": "iOS Podfile.lock 갱신 필요 (pod install) — "
                          "iOS 빌드 전 미완성"}
        Path(_fc.op_fastcheck_path("op-pod")).write_text(
            json.dumps({"ok": True, "pod_drift": drift}),
            encoding="utf-8")
        out = batches._batch_pod_drift(
            self._batch(origin_m2="op-pod"))
        self.assertEqual(out, drift)

    def test_live_fallback_without_receipt(self):
        out = batches._batch_pod_drift(self._batch())
        self.assertIsNotNone(out)
        self.assertEqual(out["pod"], "RNVVideo")
        self.assertEqual(out["pinned"], "6.19.2")

    def test_quiet_when_pin_current(self):
        (self.wt / "example" / "ios" / "Podfile.lock").write_text(
            "PODS:\n  - RNVVideo (6.19.3):\n", encoding="utf-8")
        self.assertIsNone(batches._batch_pod_drift(self._batch()))

    def test_second_primary_labels_joint(self):
        # M2.22: the first primary (expo) is pod-quiet; the
        # second (rnv-pkg) is drifted. The live fallback checks
        # every primary, not only one.
        out = batches._batch_pod_drift(self._batch(primaries=[
            {"dep": "expo", "target": "58.0.19", "lane": "next"},
            {"dep": "rnv-pkg", "target": "6.19.3",
             "lane": "next"}]))
        self.assertIsNotNone(out)
        self.assertEqual(out["pod"], "RNVVideo")
        self.assertEqual(out["pinned"], "6.19.2")

    def test_null_receipt_gives_per_pod_command(self):
        # M2.23 live case: an older receipt with pod_drift null
        # and an in-sync RN tree — the card names only the
        # drifted pod, with no failure claim.
        import json as _json
        from openmainty import fastcheck as _fc
        (self.wt / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.86.2"}}),
            encoding="utf-8")
        (self.wt / "example" / "ios" / "Podfile.lock").write_text(
            "PODS:\n"
            "  - RNVVideo (6.19.2):\n"
            "  - ReactNativeDependencies (0.86.2):\n"
            "  - React-Core-prebuilt (0.86.2):\n",
            encoding="utf-8")
        Path(_fc.op_fastcheck_path("op-video")).write_text(
            _json.dumps({"ok": True, "pod_drift": None}),
            encoding="utf-8")
        out = batches._batch_pod_drift(
            self._batch(origin_m2="op-video"))
        self.assertIsNotNone(out)
        cmd = ("cd example/ios && pod update RNVVideo "
               "--no-repo-update")
        self.assertEqual(out["command"], cmd)
        self.assertEqual(
            out["label"],
            "iOS Podfile.lock 갱신 필요 — `" + cmd + "`")

    def test_mixed_command_appends_pod_after_prebuilt(self):
        # M2.23: the tree also carries RN prebuilt drift — one
        # command, prebuilt pods first, then the drifted pod.
        import json as _json
        (self.wt / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.88.0"}}),
            encoding="utf-8")
        (self.wt / "example" / "ios" / "Podfile.lock").write_text(
            "PODS:\n"
            "  - RNVVideo (6.19.2):\n"
            "  - ReactNativeDependencies (0.86.2):\n"
            "  - React-Core-prebuilt (0.86.2):\n"
            "  - hermes-engine (0.86.2):\n",
            encoding="utf-8")
        out = batches._batch_pod_drift(self._batch())
        self.assertIsNotNone(out)
        cmd = ("cd example/ios && pod update "
               "ReactNativeDependencies React-Core-prebuilt "
               "hermes-engine RNVVideo --no-repo-update")
        self.assertEqual(out["command"], cmd)
        self.assertIn("pod install만으로는 실패", out["label"])

    def test_rn_joint_label_is_byte_identical(self):
        # M2.23: the RN 0.88 joint card text does not change.
        import json as _json
        from openmainty import fastcheck as _fc
        wt2 = Path(self.tmp.name) / "wt-joint"
        ios = wt2 / "example" / "ios"
        ios.mkdir(parents=True)
        (ios / "Podfile.lock").write_text(
            "PODS:\n"
            "  - ReactNativeDependencies (0.86.2):\n"
            "  - React-Core-prebuilt (0.86.2):\n"
            "  - hermes-engine (0.86.2):\n",
            encoding="utf-8")
        (wt2 / "example" / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.88.0"}}),
            encoding="utf-8")
        Path(_fc.op_fastcheck_path("op-rnjoint")).write_text(
            _json.dumps({"ok": True, "pod_drift": None}),
            encoding="utf-8")
        batch = self._batch(
            origin_m2="op-rnjoint",
            primaries=[{"dep": "react-native", "target": "0.88.0",
                        "lane": "next"}],
            integrations=[{"lane": "next",
                           "worktree": str(wt2)}])
        out = batches._batch_pod_drift(batch)
        cmd = ("cd example/ios && pod update "
               "ReactNativeDependencies React-Core-prebuilt "
               "hermes-engine --no-repo-update")
        self.assertEqual(
            out["label"],
            "iOS Podfile.lock 갱신 필요 — `" + cmd +
            "` (pod install만으로는 실패)")

    def test_tree_rn_labels_through_null_receipt(self):
        # M2.22 live case: an older joint receipt with pod_drift
        # null, a quiet single primary, but the tree carries RN
        # 0.88.0 over a 0.86.2 prebuilt lock.
        import json as _json
        from openmainty import fastcheck as _fc
        wt2 = Path(self.tmp.name) / "wt2"
        ios = wt2 / "example" / "ios"
        ios.mkdir(parents=True)
        (ios / "Podfile.lock").write_text(
            "PODS:\n"
            "  - ReactNativeDependencies (0.86.2)\n"
            "  - React-Core-prebuilt (0.86.2):\n"
            "  - hermes-engine (0.86.2):\n",
            encoding="utf-8")
        (wt2 / "example" / "package.json").write_text(_json.dumps(
            {"dependencies": {"react-native": "0.88.0"}}),
            encoding="utf-8")
        Path(_fc.op_fastcheck_path("op-joint")).write_text(
            _json.dumps({"ok": True, "pod_drift": None}),
            encoding="utf-8")
        batch = self._batch(
            origin_m2="op-joint",
            primaries=[{"dep": "expo", "target": "58.0.19",
                        "lane": "next"}],
            integrations=[{"lane": "next",
                           "worktree": str(wt2)}])
        out = batches._batch_pod_drift(batch)
        self.assertIsNotNone(out)
        self.assertIn("pod update", out["label"])
        self.assertIn("React-Core-prebuilt",
                      out.get("command") or "")

    def test_in_sync_tree_stays_quiet(self):
        import json as _json
        (self.wt / "example" / "package.json").write_text(
            _json.dumps(
                {"dependencies": {"react-native": "0.86.2"}}),
            encoding="utf-8")
        (self.wt / "example" / "ios" / "Podfile.lock").write_text(
            "PODS:\n  - RNVVideo (6.19.3):\n", encoding="utf-8")
        self.assertIsNone(batches._batch_pod_drift(self._batch(
            primaries=[{"dep": "expo", "target": "58.0.19",
                        "lane": "next"}])))

    def test_card_renders_pod_and_dupes(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        self.assertIn("b.pod_drift", src)
        self.assertIn("같은 패키지 두 버전", src)


class VocabTests(ManifestFixture):
    """L2 live fix: base batches use different gate vocabularies —
    the nitro profile maps onto companions everywhere."""

    def base_with(self, required):
        base = json.loads(batches.batch_file("base-1").read_text(
            encoding="utf-8"))
        base["policy"] = {
            "required_gates": required,
            "gates": {name: {"root": True} for name in required}}
        write_json(batches.batch_file("base-1"), base)

    def nitro_profile(self):
        return {"steps": [
            {"argv": ["yarn", "install", "--immutable"],
             "gate": "install"},
            {"argv": ["yarn", "prepare"], "gate": "prepare"},
            {"argv": ["yarn", "typecheck"], "gate": "typecheck"},
            {"argv": ["yarn", "lint"], "gate": "lint"},
            {"argv": ["yarn", "test"], "gate": "unit"}]}

    def check_vocab(self, required):
        self.base_with(required)
        m = m2.build_batch_manifest(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, self.nitro_profile())
        by_key = {g["key"]: g
                  for i in m["integrations"] for g in i["gates"]}
        for gate in ("install", "prepare", "typecheck", "lint",
                     "unit"):
            self.assertEqual(by_key["worker:" + gate]["status"],
                             "pass")
        for name in required:
            self.assertEqual(by_key[name]["status"], "pending")
        rec = batches.import_batch(m)
        live, _ = batches.live_map_with_diag([rec])
        res = batches.evaluate(rec, live)
        self.assertFalse(res["results"][0]["ready"])
        return rec

    def test_main_devtools_vocab(self):
        # immutable-install/package-build vocabulary, 10 root gates.
        self.check_vocab(["immutable-install", "package-build",
                          "unit", "typecheck", "lint", "loaded-config",
                          "peer-explain", "diffcheck", "source-review",
                          "root-qa"])

    def test_old_base_without_install(self):
        self.check_vocab(["unit", "typecheck", "lint", "diffcheck",
                          "source-review", "root-qa"])


class DupSkipTests(ManifestFixture):
    """M2.4c live fix: a candidate already riding an existing
    batch is a skip with a grouped note — never a block."""

    def test_duplicate_candidate_skips_without_import(self):
        st = store.load(self.run_id)
        st["dep"] = "jest"
        st["target"] = "30.5.2"
        store.save(st)
        write_json(batches.batch_file("have-it"), {
            "id": "have-it", "repo": "fakerepo",
            "target_branch": "next", "purpose": "hand",
            "updated_at": "2026-10-22T09:00:00Z",
            "origin": {"checkout": "/c", "base_branch": "main",
                       "base_head": "h"},
            "primaries": [{"dep": "jest", "target": "30.5.2",
                           "lane": "next"}],
            "inherited": [], "integrations": [],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}}})
        m2.set_policy("fakerepo", "batch", True)
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, _import=lambda m: self.fail("must not import"))
        self.assertFalse(out.get("ran"))
        self.assertTrue(
            str(out.get("reason") or "").startswith("dup of "))
        self.assertIn("have-it", str(out.get("reason") or ""))
        with sched.locked("fakerepo"):
            blocks = (sched.load("fakerepo").get("m2_consec_blocks")
                      or {})
        self.assertEqual(blocks, {})

    def test_find_candidate_batch_prefers_newest(self):
        for bid, when in (("old-one", "2026-10-20T00:00:00Z"),
                          ("new-one", "2026-10-22T09:00:00Z")):
            write_json(batches.batch_file(bid), {
                "id": bid, "repo": "fakerepo", "updated_at": when,
                "primaries": [{"dep": "jest", "target": "30.5.2",
                               "lane": "next"}],
                "integrations": [],
                "policy": {"required_gates": ["unit"],
                           "gates": {"unit": {}}}})
        self.assertEqual(
            batches.find_candidate_batch("fakerepo", "jest",
                                         "30.5.2", "next"),
            "new-one")
        self.assertEqual(
            batches.find_candidate_batch("fakerepo", "jest",
                                         "30.5.2", "main"), "")
        self.assertEqual(
            batches.find_candidate_batch("fakerepo", "nope",
                                         "1.0", "next"), "")


class CompanionStageTests(ManifestFixture):
    def test_pending_roots_never_count_a_block(self):
        # A registered auto batch with pending Root gates is
        # WAITING, not a failure: no consec-block count, no lane
        # cooldown fodder.
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        out = m2._run_batch_stage(
            "fakerepo", self.run_id, "next", self.rec, "base-1",
            self.fc, _import=lambda m: {"id": m["id"]})
        self.assertTrue(out.get("ran"))
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})


class RetryTests(AttestFixture):
    """L2: owner/Root re-run of a RED fastcheck receipt via
    {op, confirm:true, retry:true} — archive kept, attempts listed,
    and a passing re-run clears exactly the stamped contribution."""

    def oprec(self, op="op-retry-1"):
        from openmainty import fastcheck as _fc
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        write_json(ops / (op + ".json"), {
            "op": "prepare", "op_id": op, "status": "done",
            "run_id": self.run_id, "dep": "jest", "target": "30.5.2",
            "result": {"path": str(self.origin),
                       "branch": "codex/cand-1", "head": self.head,
                       "fingerprint": self.fp}})
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["echo", "x"],
                                             "gate": "unit"}],
                                  "toolchain": []}})
        return op

    def write_receipt(self, op, ok=False):
        from openmainty import fastcheck as _fc
        home = Path(os.environ["OPENMAINTY_HOME"])
        path = Path(str(_fc.op_fastcheck_path(op, home)))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {"ok": ok, "steps": []})
        return path

    def runner(self, ok):
        def fake(op_id, worktree, profile, head, fp, origin="ui"):
            return {"ok": ok, "steps": [],
                    "failed": None if ok else {"argv": ["x"]}}
        return fake

    def consec(self):
        return (sched.load("fakerepo").get("m2_consec_blocks") or {})

    def set_consec(self, lane, count):
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            blocks = dict(st.get("m2_consec_blocks") or {})
            blocks[lane] = count
            st["m2_consec_blocks"] = blocks
            sched.save(st)

    def test_no_retry_refuses_existing(self):
        op = self.oprec()
        self.write_receipt(op, ok=False)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.run_fastcheck_for_op(op, _runner=self.runner(True))
        self.assertIn("다시 실행하지 않습니다", str(ctx.exception))

    def test_retry_on_green_refuses(self):
        op = self.oprec()
        self.write_receipt(op, ok=True)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.run_fastcheck_for_op(op, retry=True,
                                         _runner=self.runner(True))
        self.assertIn("이미 통과함", str(ctx.exception))

    def test_retry_archives_and_reruns(self):
        op = self.oprec()
        first = self.write_receipt(op, ok=False)
        out = batches.run_fastcheck_for_op(op, retry=True,
                                           _runner=self.runner(True))
        self.assertTrue(out["ok"])
        self.assertTrue(out["retried"])
        self.assertTrue(
            out["archived_receipt"].endswith(".attempt-1.json"))
        self.assertFalse(out["block_released"])  # no stamp existed
        self.assertFalse(first.exists())
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        archived = ops / out["archived_receipt"]
        self.assertTrue(archived.is_file())
        rec = json.loads((ops / (op + ".json")).read_text(
            encoding="utf-8"))
        kinds = [a.get("kind") for a in rec.get("attempts") or []]
        self.assertIn("fastcheck-retry", kinds)

    def test_second_retry_is_attempt_2(self):
        op = self.oprec()
        self.write_receipt(op, ok=False)
        out1 = batches.run_fastcheck_for_op(op, retry=True,
                                            _runner=self.runner(True))
        self.assertTrue(out1["archived_receipt"].endswith(
            ".attempt-1.json"))
        self.write_receipt(op, ok=False)
        out2 = batches.run_fastcheck_for_op(op, retry=True,
                                            _runner=self.runner(True))
        self.assertTrue(out2["archived_receipt"].endswith(
            ".attempt-2.json"))

    def test_retry_pass_clears_stamped_contribution(self):
        op = self.oprec()
        self.write_receipt(op, ok=False)
        self.assertTrue(batches.stamp_counted_block(op, "next"))
        self.set_consec("next", 1)
        out = batches.run_fastcheck_for_op(op, retry=True,
                                           _runner=self.runner(True))
        self.assertTrue(out["block_released"])
        self.assertEqual(self.consec().get("next"), 0)

    def test_retry_fail_keeps_count_and_stamp(self):
        op = self.oprec()
        self.write_receipt(op, ok=False)
        self.assertTrue(batches.stamp_counted_block(op, "next"))
        self.set_consec("next", 1)
        out = batches.run_fastcheck_for_op(op, retry=True,
                                           _runner=self.runner(False))
        self.assertFalse(out["ok"])
        self.assertFalse(out["block_released"])
        self.assertEqual(self.consec().get("next"), 1)

    def test_attempts_cap_refuses_before_rename(self):
        op = self.oprec()
        self.write_receipt(op, ok=False)
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        rec = json.loads((ops / (op + ".json")).read_text(
            encoding="utf-8"))
        rec["attempts"] = [{"kind": "x"}] * batches._MAX_ATTEMPTS
        write_json(ops / (op + ".json"), rec)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.run_fastcheck_for_op(op, retry=True,
                                         _runner=self.runner(True))
        self.assertIn("재시도 횟수 초과", str(ctx.exception))
        # Refused before the rename: the live receipt is untouched.
        from openmainty import fastcheck as _fc
        home = Path(os.environ["OPENMAINTY_HOME"])
        self.assertTrue(Path(str(_fc.op_fastcheck_path(
            op, home))).is_file())


class RerunTests(AttestFixture):
    """M2.16c: one explicit human re-verification of a done prepare
    op whose receipt predates a new gate. The plain manual run
    answers 409 while any receipt exists; the rerun archives the
    old receipt (prev-stamp name, never deleted) plus its step
    logs, runs once, and records "검증 다시 실행 (by …)"."""

    def oprec(self, op="op-rerun-1", status="done", opkind="prepare"):
        from openmainty import fastcheck as _fc
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        write_json(ops / (op + ".json"), {
            "op": opkind, "op_id": op, "status": status,
            "run_id": self.run_id, "dep": "jest", "target": "30.5.2",
            "result": {"path": str(self.origin),
                       "branch": "codex/cand-1", "head": self.head,
                       "fingerprint": self.fp}})
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["echo", "x"],
                                             "gate": "unit"}],
                                  "toolchain": []}})
        return op

    def write_receipt(self, op, ok=True):
        from openmainty import fastcheck as _fc
        home = Path(os.environ["OPENMAINTY_HOME"])
        ops = home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        path = Path(str(_fc.op_fastcheck_path(op, home)))
        write_json(path, {"ok": ok, "steps": []})
        (ops / (op + ".fastcheck.0.log")).write_text(
            "step0\n", encoding="utf-8")
        (ops / (op + ".fastcheck.consumer.log")).write_text(
            "consumer\n", encoding="utf-8")
        return path

    def runner(self, ok, write=True):
        from openmainty import fastcheck as _fc

        def fake(op_id, worktree, profile, head, fp, origin="ui"):
            if write:
                home = Path(os.environ["OPENMAINTY_HOME"])
                write_json(Path(str(_fc.op_fastcheck_path(
                    op_id, home))),
                    {"ok": ok, "steps": []})
            return {"ok": ok, "steps": [],
                    "failed": None if ok else {"argv": ["x"]}}
        return fake

    def live_receipt(self, op):
        from openmainty import fastcheck as _fc
        home = Path(os.environ["OPENMAINTY_HOME"])
        return Path(str(_fc.op_fastcheck_path(op, home)))

    def test_worker_400_starts_nothing(self):
        op = self.oprec()
        live = self.write_receipt(op)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rerun_fastcheck_for_op(op, by="worker",
                                           _runner=self.runner(True))
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertTrue(live.is_file())

    def test_unknown_op_409(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rerun_fastcheck_for_op("op-nope-9", by="root",
                                           _runner=self.runner(True))
        self.assertEqual(ctx.exception.http_status, 409)

    def test_not_done_409(self):
        op = self.oprec(status="blocked")
        self.write_receipt(op)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rerun_fastcheck_for_op(op, by="root",
                                           _runner=self.runner(True))
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertTrue(self.live_receipt(op).is_file())

    def test_non_prepare_409(self):
        op = self.oprec(opkind="rollup")
        self.write_receipt(op)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rerun_fastcheck_for_op(op, by="owner",
                                           _runner=self.runner(True))
        self.assertEqual(ctx.exception.http_status, 409)

    def test_busy_op_409_writes_no_receipt_or_event(self):
        from openmainty import events as _events
        op = self.oprec()
        live = self.write_receipt(op)

        def busy(op_id, worktree, profile, head, fp, origin="ui"):
            raise m2.OpBusy(op_id)

        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rerun_fastcheck_for_op(op, by="root",
                                           _runner=busy)
        self.assertEqual(ctx.exception.http_status, 409)
        # The archive already kept the old receipt (same as the
        # L2 retry path), but the busy op wrote no new receipt
        # and recorded no rerun event.
        self.assertFalse(live.is_file())
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        self.assertEqual(len(sorted(ops.glob(
            op + ".fastcheck.prev-*.json"))), 1)
        self.assertEqual(
            [e for e in _events.list_events("fakerepo")
             if e.get("kind") == "m2-fastcheck-rerun"], [])

    def test_success_archives_prev_and_reruns_with_event(self):
        from openmainty import events as _events
        op = self.oprec()
        self.write_receipt(op, ok=True)
        out = batches.rerun_fastcheck_for_op(
            op, by="root", _runner=self.runner(True))
        self.assertTrue(out["ok"])
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        prev = sorted(p.name for p in ops.glob(
            op + ".fastcheck.prev-*.json"))
        self.assertEqual(len(prev), 1)
        self.assertTrue(out["archived_receipt"] == prev[0])
        # The old receipt still exists under the prev name; the
        # run wrote a fresh receipt at the live path.
        prev_rec = json.loads((ops / prev[0]).read_text(
            encoding="utf-8"))
        self.assertTrue(prev_rec["ok"])
        live_rec = json.loads(self.live_receipt(op).read_text(
            encoding="utf-8"))
        self.assertTrue(live_rec["ok"])
        # Step logs rode the same prev stamp, old names gone.
        archived_logs = sorted(out["archived_logs"])
        self.assertEqual(len(archived_logs), 2)
        self.assertTrue(all(".prev-" in name
                            for name in archived_logs))
        self.assertFalse((ops / (op + ".fastcheck.0.log")).exists())
        self.assertFalse(
            (ops / (op + ".fastcheck.consumer.log")).exists())
        found = [e for e in _events.list_events("fakerepo")
                 if e.get("kind") == "m2-fastcheck-rerun"]
        self.assertTrue(found)
        self.assertIn("검증 다시 실행 (by root)",
                      found[-1].get("title") or "")
        self.assertIn(op, found[-1].get("detail") or "")

    def test_rerun_without_receipt_runs_and_records(self):
        from openmainty import events as _events
        op = self.oprec()
        out = batches.rerun_fastcheck_for_op(
            op, by="owner", _runner=self.runner(False))
        self.assertFalse(out["ok"])
        self.assertEqual(out["archived_receipt"], "")
        found = [e for e in _events.list_events("fakerepo")
                 if e.get("kind") == "m2-fastcheck-rerun"]
        self.assertTrue(found)
        self.assertIn("(by owner)", found[-1].get("title") or "")

    def test_driver_never_calls_rerun(self):
        import pathlib as _pl
        src = (_pl.Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "m2.py").read_text(encoding="utf-8")
        self.assertNotIn("rerun_fastcheck", src)

    def test_two_reruns_keep_distinct_prev_archives(self):
        # M2.16b-A1: the second rerun archives under its own stamp
        # and never re-renames the first archive's logs away from
        # their receipt.
        op = self.oprec()
        self.write_receipt(op, ok=True)
        out1 = batches.rerun_fastcheck_for_op(
            op, by="root", _runner=self.runner(True), _stamp="s1")
        self.assertEqual(out1["archived_receipt"],
                         op + ".fastcheck.prev-s1.json")
        self.assertEqual(len(out1["archived_logs"]), 2)
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        # The new run wrote a fresh receipt; give it fresh logs, as
        # a real run would, then rerun again.
        (ops / (op + ".fastcheck.0.log")).write_text(
            "step0-again\n", encoding="utf-8")
        out2 = batches.rerun_fastcheck_for_op(
            op, by="root", _runner=self.runner(True), _stamp="s2")
        self.assertEqual(out2["archived_receipt"],
                         op + ".fastcheck.prev-s2.json")
        names = sorted(p.name for p in ops.iterdir())
        # First archive intact under its own names.
        self.assertIn(op + ".fastcheck.prev-s1.json", names)
        self.assertIn(op + ".fastcheck.prev-s1.0.log", names)
        self.assertIn(op + ".fastcheck.prev-s1.consumer.log", names)
        # Second archive got its own names; nothing was renamed
        # twice (no prev-s2.prev-s1 chains).
        self.assertIn(op + ".fastcheck.prev-s2.json", names)
        self.assertIn(op + ".fastcheck.prev-s2.0.log", names)
        self.assertFalse(any("prev-s2" in n and "prev-s1" in n
                             for n in names))

    def test_unreadable_receipt_archives_before_run(self):
        # M2.16b-A2: a receipt that is not a dict (here corrupt
        # bytes) still moves aside under a prev name — the new run
        # never overwrites it.
        op = self.oprec()
        live = self.live_receipt(op)
        live.write_bytes(b"not json{{{")
        out = batches.rerun_fastcheck_for_op(
            op, by="owner", _runner=self.runner(True), _stamp="s9")
        self.assertEqual(out["archived_receipt"],
                         op + ".fastcheck.prev-s9.json")
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        prev = (ops / out["archived_receipt"]).read_bytes()
        self.assertEqual(prev, b"not json{{{")
        self.assertTrue(live.is_file())

    def test_existing_destination_refuses_before_anything_runs(self):
        # M2.16b-A3: POSIX rename would overwrite silently, so an
        # existing archive name refuses — the live receipt stays,
        # the runner never starts, no event is recorded.
        from openmainty import events as _events
        op = self.oprec()
        live = self.write_receipt(op, ok=True)
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        (ops / (op + ".fastcheck.prev-S.json")).write_text(
            "older archive", encoding="utf-8")
        calls = []

        def tracking(op_id, worktree, profile, head, fp, origin="ui"):
            calls.append(op_id)
            return {"ok": True, "steps": [], "failed": None}

        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.rerun_fastcheck_for_op(
                op, by="root", _runner=tracking, _stamp="S")
        self.assertIn("이미 있음", str(ctx.exception))
        self.assertTrue(live.is_file())
        self.assertEqual(calls, [])
        self.assertEqual(
            [e for e in _events.list_events("fakerepo")
             if e.get("kind") == "m2-fastcheck-rerun"], [])


class SingleSummaryDirTests(AttestFixture):
    """M2.16b-B1: the snapshot writer resolves the summary dir once.

    Tmp, prev and target build from that single Path, so they are
    always siblings — even when HOME flips mid-run (the live leak:
    rows computed in a temp home, re-resolved after the flip, and
    snapshot.<pid>.tmp orphaned in the real home). A failed
    write/replace removes only that call's own tmp.
    """

    def test_home_flip_keeps_tmp_beside_target(self):
        home_a = Path(os.environ["OPENMAINTY_HOME"])
        home_b = Path(self.tmp.name) / "home-b"
        home_b.mkdir()
        real_list_all = repos.list_all

        def flip():
            os.environ["OPENMAINTY_HOME"] = str(home_b)
            return real_list_all()

        repos.list_all = flip
        try:
            batches.refresh_summary_snapshot("test-flip")
        finally:
            repos.list_all = real_list_all
        # Resolved once at the top: the snapshot lands in the
        # first home, nothing in the second, no tmp anywhere.
        self.assertTrue(
            (home_a / "summary" / "snapshot.json").is_file())
        self.assertFalse(
            (home_b / "summary" / "snapshot.json").exists())
        for home in (home_a, home_b):
            sdir = home / "summary"
            if sdir.is_dir():
                self.assertEqual(list(sdir.glob("*.tmp")), [])

    def test_failed_replace_removes_only_own_tmp(self):
        from unittest import mock as _mock
        home = Path(os.environ["OPENMAINTY_HOME"])
        (home / "summary").mkdir(parents=True, exist_ok=True)
        with _mock.patch.object(Path, "replace",
                                side_effect=OSError("disk")):
            batches.refresh_summary_snapshot("test-boom")
        sdir = home / "summary"
        self.assertEqual(list(sdir.glob("*.tmp")), [])
        self.assertFalse((sdir / "snapshot.json").exists())


class Phase1DupTests(unittest.TestCase):
    """M2.4d: already-batched resumes never consume the per-tick cap.

    Live shape: 12 verified ops already ride an existing batch as
    primaries. Without the Phase 1 drop, cap 1 spends its only slot
    on a dup skip every tick and fresh batch work starves.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        for name in ("latest_integration_op", "candidate_proof_error",
                     "default_base"):
            real = getattr(batches, name)
            self.addCleanup(setattr, batches, name, real)
        self.op_recs = {}
        batches.latest_integration_op = lambda rid: self.op_recs.get(rid)
        batches.candidate_proof_error = lambda rid: None
        batches.default_base = lambda repo, lane, **k: {"id": "base-1"}
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        (Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops").mkdir(
            parents=True, exist_ok=True)
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "batch", True)
        # FIX-a1: these mechanics tests run with the full
        # prepare -> fastcheck -> batch prefix on (a disabled
        # predecessor must stop the batch stage). Verification
        # itself stays stubbed not-ran, so fresh items behave
        # exactly as under the old fastcheck-off tick.
        m2.set_policy("fakerepo", "fastcheck", True)
        real_fc_stage = m2._run_fastcheck_stage
        self.addCleanup(setattr, m2, "_run_fastcheck_stage",
                        real_fc_stage)
        m2._run_fastcheck_stage = (
            lambda *a, **k: {"ran": False, "reason": "stub"})
        self.prepared = []

    def _mk_run(self, dep, target, lane="next", with_rec=False,
                created="2026-10-20T00:00:00+00:00",
                kind="dep-update"):
        run = store.create("fakerepo", "issue", "t", None, {"by": "t"})
        rid = run["id"]
        st = store.load(rid)
        st["dep"] = dep
        st["target"] = target
        st["lane"] = lane
        store.save(st)
        if with_rec:
            op_id = f"prep-{rid}-base-1"
            self.op_recs[rid] = {
                "op": "prepare", "op_id": op_id, "run_id": rid,
                "status": "done", "batch_id": "base-1",
                "result": {"path": "/w", "branch": "codex/x",
                           "head": "a" * 40,
                           "fingerprint": "b" * 64}}
            from openmainty import fastcheck as _fc
            Path(_fc.op_fastcheck_path(op_id)).write_text(
                json.dumps({"ok": True, "steps": []}), encoding="utf-8")
        with sched.locked("fakerepo"):
            qst = sched.load("fakerepo")
            qst.setdefault("queue", []).append(
                {"run_id": rid, "status": "waiting-review", "lane": lane,
                 "dep": dep, "target": target, "verified": True,
                 "kind": kind, "created_at": created})
            sched.save(qst)
        return rid

    def _mk_hand_batch(self, pairs, bid="hand-1"):
        write_json(batches.batch_file(bid),
                   {"id": bid, "repo": "fakerepo",
                    "updated_at": "2026-10-22T00:00:00+00:00",
                    "primaries": [{"dep": d, "target": t, "lane": "next"}
                                  for d, t in pairs]})

    def _fake_prepare(self, run_id, base_id, origin="m2"):
        self.prepared.append(run_id)
        return {"op": "prepare",
                "op_id": f"prep-{run_id}-base-1", "run_id": run_id,
                "status": "done",
                "result": {"branch": "codex/fresh", "head": "c" * 40}}

    def _titles(self):
        return [e.get("title") for e in events.list_events("fakerepo")]

    def test_dup_resumes_never_consume_cap(self):
        pairs = [(f"dup-dep-{i}", f"1.{i}") for i in range(12)]
        for i, (dep, target) in enumerate(pairs):
            self._mk_run(dep, target, with_rec=True,
                         created=f"2026-10-20T00:00:{i:02d}+00:00")
        self._mk_hand_batch(pairs)
        fresh = self._mk_run("fresh-dep", "9.9", with_rec=False,
                             created="2026-10-22T00:00:00+00:00")
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare)
        # The only slot went to the fresh candidate, never a dup.
        self.assertEqual(self.prepared, [fresh])
        dups = [s for s in out["skipped"]
                if str(s.get("reason") or "").startswith("dup of ")]
        self.assertEqual(len(dups), 12)
        self.assertEqual(
            [e for e in events.list_events("fakerepo")
             if e.get("kind") == "m2-blocked"], [])
        self.assertIn("자율 묶음 건너뜀 12건: 이미 묶음 hand-1에 포함",
                      self._titles())
        # Next tick: duplicates are still not work — no re-emit, and
        # the fresh fake (which writes no record) prepares again.
        out2 = m2.tick_repo("fakerepo", _running="promoted",
                            _prepare=self._fake_prepare)
        self.assertEqual(self.prepared, [fresh, fresh])
        self.assertEqual(
            self._titles().count(
                "자율 묶음 건너뜀 12건: 이미 묶음 hand-1에 포함"), 1)
        dups2 = [s for s in out2["skipped"]
                 if str(s.get("reason") or "").startswith("dup of ")]
        self.assertEqual(len(dups2), 12)

    def test_two_nondup_resumes_batch_across_two_ticks(self):
        pairs = [(f"dup-dep-{i}", f"1.{i}") for i in range(12)]
        for dep, target in pairs:
            self._mk_run(dep, target, with_rec=True)
        self._mk_hand_batch(pairs)
        first = self._mk_run("new-a", "2.0", with_rec=True,
                             created="2026-10-21T00:00:00+00:00")
        second = self._mk_run("new-b", "3.0", with_rec=True,
                              created="2026-10-21T01:00:00+00:00")
        calls = []
        real_stage = m2._run_batch_stage
        self.addCleanup(setattr, m2, "_run_batch_stage", real_stage)

        def fake_stage(repo, run_id, lane, rec, base_id, summary,
                       _import=None, extra_primaries=None):
            calls.append(run_id)
            # Persist what a real import would: the next tick sees
            # the batch and leaves this run alone.
            write_json(
                batches.batch_file(m2.batch_id_for_op(
                    str((rec or {}).get("op_id") or ""))),
                {"id": "auto-x", "repo": "fakerepo",
                 "updated_at": "2026-10-22T01:00:00+00:00",
                 "primaries": []})
            return {"ran": True, "batch": f"auto-{run_id}"}
        m2._run_batch_stage = fake_stage
        out1 = m2.tick_repo("fakerepo", _running="promoted",
                            _prepare=self._fake_prepare)
        # Tick 1 batches the oldest non-dup; the slot is not spent
        # on any of the 12 duplicates.
        self.assertEqual(calls, [first])
        self.assertEqual(self.prepared, [])
        out2 = m2.tick_repo("fakerepo", _running="promoted",
                            _prepare=self._fake_prepare)
        self.assertEqual(calls, [first, second])
        self.assertEqual(self.prepared, [])
        self.assertIn("자율 묶음 건너뜀 12건: 이미 묶음 hand-1에 포함",
                      self._titles())


class LockstepDriverTests(Phase1DupTests):
    """M2.5b: the RN-lockstep family never prepares or batches alone."""

    def _base_tree(self, rn="0.86.2", metro="0.86.2"):
        wt = Path(self.tmp.name) / f"base-{rn}"
        (wt / "example").mkdir(parents=True, exist_ok=True)
        (wt / "package.json").write_text(
            json.dumps({"dependencies": {"react-native": rn}}),
            encoding="utf-8")
        (wt / "example" / "package.json").write_text(
            json.dumps({"dependencies": {"react-native": rn},
                        "devDependencies":
                        {"@react-native/metro-config": metro}}),
            encoding="utf-8")
        write_json(batches.batch_file("base-1"),
                   {"id": "base-1", "repo": "fakerepo",
                    "updated_at": "2026-10-22T00:00:00+00:00",
                    "primaries": [],
                    "integrations": [{"lane": "next",
                                      "worktree": str(wt)}]})
        return wt

    def test_family_fresh_skips_never_prepares(self):
        self._base_tree()
        rid = self._mk_run("@react-native/metro-config", "0.88.0",
                           with_rec=False)
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare)
        self.assertEqual(self.prepared, [])
        mine = [s for s in out["skipped"] if s.get("run") == rid]
        self.assertEqual(len(mine), 1)
        self.assertIn("함께 옮겨야 함", mine[0]["reason"])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertNotIn("m2-blocked", kinds)

    def test_same_minor_and_non_family_prepare(self):
        self._base_tree()
        a = self._mk_run("@react-native/metro-config", "0.86.5",
                         with_rec=False,
                         created="2026-10-20T00:00:00+00:00")
        b = self._mk_run("react", "19.3.1", with_rec=False,
                         created="2026-10-20T01:00:00+00:00")
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=self._fake_prepare)
        self.assertEqual(self.prepared, [a, b])

    def test_rn_done_lists_companions(self):
        self._base_tree()
        self._mk_run("react-native", "0.88.0", with_rec=False,
                     created="2026-10-20T00:00:00+00:00")
        self._mk_run("@react-native/metro-config", "0.88.0",
                     with_rec=False,
                     created="2026-10-20T01:00:00+00:00")

        def dep_prepare(run_id, base_id, origin="m2"):
            self.prepared.append(run_id)
            st = store.load(run_id)
            return {"op": "prepare",
                    "op_id": f"prep-{run_id}-base-1", "run_id": run_id,
                    "status": "done", "dep": st.get("dep"),
                    "target": st.get("target"),
                    "result": {"branch": "codex/fresh",
                               "head": "c" * 40}}
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=dep_prepare)
        details = [e.get("detail") or ""
                   for e in events.list_events("fakerepo")]
        self.assertTrue(
            any("같이 옮길 후보" in d
                and "@react-native/metro-config 0.88.0" in d
                for d in details))

    def test_stage_never_batches_family_alone(self):
        wt = self._base_tree()
        rid = self._mk_run("@react-native/metro-config", "0.88.0",
                           with_rec=True)
        rec = dict(self.op_recs[rid])
        rec["result"] = {"path": str(wt), "branch": "codex/x",
                         "head": "d" * 40, "fingerprint": "e" * 64}
        out = m2._run_batch_stage("fakerepo", rid, "next", rec,
                                  "base-1", {"ok": True, "steps": []})
        self.assertFalse(out["ran"])
        self.assertTrue(str(out["reason"]).startswith("lockstep "))
        self.assertIn("함께 옮겨야 함", out["reason"])


class LockfixResumeTests(Phase1DupTests):
    """M24d-F2: a prepare(blocked) -> lockfix(done) strand resumes.

    Live shape: run-d60b6bc9 (react-native-video, main) went
    prepare-blocked, then UI re-resolve done, then fastcheck PASS —
    but latest_integration_op returns the reresolve record, which
    both resume helpers declined as op != prepare.
    """

    def _mk_strand(self, dep="vid", target="6.19.3", lane="main",
                   receipt=True):
        rid = self._mk_run(dep, target, lane=lane, with_rec=False,
                           created="2026-10-21T00:00:00+00:00")
        src_id = f"prep-{rid}-base-1"
        write_json(batches.op_file(src_id),
                   {"op": "prepare", "op_id": src_id, "run_id": rid,
                    "status": "blocked", "batch_id": "base-1",
                    "error": "잠금 충돌"})
        der_id = src_id + "-lockfix"
        self.op_recs[rid] = {
            "op": "reresolve", "op_id": der_id,
            "derived_from": src_id, "repo": "fakerepo",
            "run_id": rid, "batch_id": "base-1",
            "dep": dep, "target": target, "lane": lane,
            "status": "done",
            "result": {"path": "/w", "branch": "codex/x-lockfix",
                       "head": "d" * 40, "fingerprint": "e" * 64}}
        if receipt:
            from openmainty import fastcheck as _fc
            import json as _json
            Path(_fc.op_fastcheck_path(der_id)).write_text(
                _json.dumps({"ok": True, "steps": []}),
                encoding="utf-8")
        return rid, der_id

    def test_lockfix_done_with_receipt_batches(self):
        rid, _ = self._mk_strand()
        calls = []
        real_stage = m2._run_batch_stage
        self.addCleanup(setattr, m2, "_run_batch_stage", real_stage)
        m2._run_batch_stage = (
            lambda repo, run_id, lane, rec, base_id, summary,
            _import=None, extra_primaries=None: calls.append(run_id)
            or {"ran": True, "batch": "auto-x"})
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare)
        self.assertEqual(calls, [rid])
        self.assertEqual(self.prepared, [])
        plains = [s for s in out["skipped"]
                  if s.get("reason") == "이미 기록된 작업 있음"]
        self.assertEqual(
            [s for s in plains if s.get("run") == rid], [])

    def test_lockfix_done_without_receipt_resumes_fastcheck(self):
        m2.set_policy("fakerepo", "fastcheck", True)
        rid, _ = self._mk_strand(receipt=False)
        got = m2._fc_resume_eligible(
            {"fastcheck": True}, rid, "main", self.op_recs[rid])
        self.assertIsNotNone(got)
        self.assertIsNone(m2._batch_resume(
            {"batch": True}, rid, "main", self.op_recs[rid]))

    def test_reresolve_without_prepare_behind_is_plain_skip(self):
        self._mk_run("vid", "6.19.3", lane="main", with_rec=False)
        lone = {"op": "reresolve", "op_id": "op-lone-lockfix",
                "run_id": "run-lone", "status": "done",
                "batch_id": "base-1",
                "result": {"path": "/w", "branch": "codex/x",
                           "head": "d" * 40, "fingerprint": "e" * 64}}
        self.assertIsNone(m2._batch_resume(
            {"batch": True}, "run-lone", "main", lone))
        self.assertIsNone(m2._fc_resume_eligible(
            {"fastcheck": True}, "run-lone", "main", lone))
        # A derived_from pointing at a non-prepare (or missing)
        # record is a plain skip too.
        bad = dict(lone, derived_from="op-lone-lockfix")
        write_json(batches.op_file("op-lone-lockfix"),
                   {"op": "reresolve", "op_id": "op-lone-lockfix",
                    "run_id": "run-lone", "status": "done"})
        self.assertIsNone(m2._batch_resume(
            {"batch": True}, "run-lone", "main", bad))


class JointExemptFixture(ManifestFixture):
    """M2.14c: a done joint import next to a held standalone batch
    built from one partner's op."""

    def setUp(self):
        super().setUp()
        self.co = Path(self.tmp.name) / "checkout"
        self.base_head = batches.load_batch("base-1")["origin"]["base_head"]
        self.x_run = self.run_id
        self.x_head = self.rec["result"]["head"]
        self.x_fp = self.rec["result"]["fingerprint"]
        self.x_wt = Path(self.rec["result"]["path"])
        # Y run: expo 58.0.17 with its own worktree on codex/run-y.
        run = store.create("fakerepo", "expo@58.0.17", "t", None,
                           {"by": "t"})
        self.y_run = run["id"]
        st = store.load(self.y_run)
        st["dep"] = "expo"
        st["target"] = "58.0.17"
        store.save(st)
        self.y_wt = store.run_dir(self.y_run) / "work"
        self.y_wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", str(self.y_wt),
                        "-b", "codex/run-y"],
                       cwd=str(self.co), check=True,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        snap = runner.source_snapshot(self.y_wt)
        st = store.load(self.y_run)
        st["workCommit"] = snap["head"]
        st["workTreeFingerprint"] = snap["fingerprint"]
        store.save(st)
        self.y_head, self.y_fp = snap["head"], snap["fingerprint"]
        qst = sched.load("fakerepo")
        qst.setdefault("queue", []).append(
            {"key": "ky", "repo": "fakerepo", "dep": "expo",
             "target": "58.0.17", "lane": "next", "status": "waiting-review",
             "run_id": self.y_run, "branch": "codex/run-y"})
        sched.save(qst)
        _bind_accept(self.x_run, self.x_head, self.x_fp)
        _bind_accept(self.y_run, self.y_head, self.y_fp)
        self.x_op = "prep-%s-base-1" % self.x_run
        self.y_op = "prep-%s-base-1" % self.y_run
        self.y_bid = m2.batch_id_for_op(self.y_op)
        self.joint_op = self.x_op + "-joint"
        self.joint_bid = "auto-joint-1"
        write_json(batches.op_file(self.y_op), {
            "op": "prepare", "op_id": self.y_op, "repo": "fakerepo",
            "run_id": self.y_run, "batch_id": "base-1", "dep": "expo",
            "target": "58.0.17", "lane": "next", "status": "blocked",
            "error": "pick"})
        write_json(batches.op_file(self.joint_op), {
            "op": "reresolve", "op_id": self.joint_op,
            "derived_from": self.x_op, "kind": "joint",
            "joint_with": [self.y_op],
            "runs": [self.x_run, self.y_run],
            "repo": "fakerepo", "run_id": self.x_run,
            "batch_id": "base-1", "dep": "jest", "target": "30.5.2",
            "lane": "next", "status": "done", "error": "",
            "result": {"path": str(self.x_wt), "branch": "codex/run-1",
                       "head": self.x_head,
                       "fingerprint": self.x_fp}})

    def _primary(self, dep, target, run_id, wt, head, fp,
                   branch=None):
        if branch is None:
            branch = ("codex/run-1" if run_id == self.x_run
                      else "codex/run-y")
        return {"dep": dep, "target": target, "lane": "next",
                "run_id": run_id,
                "source": {"worktree": str(wt), "branch": branch,
                           "head": head, "fingerprint": fp},
                "review": {"verdict": "accept"}}

    def _gates(self, head, fp):
        return [{"key": "unit", "status": "pass",
                 "inputs": {"head": head, "fingerprint": fp},
                 "evidence": [{"label": "log", "kind": "file",
                               "origin": str(self.log)}]}]

    def _manifest(self, bid, primaries, wt, origin_m2=None,
                  head=None, fp=None):
        manifest = {
            "id": bid, "repo": "fakerepo", "target_branch": "next",
            "purpose": "joint test",
            "origin": {"checkout": str(wt), "base_branch": "main",
                       "base_head": self.base_head},
            "primaries": primaries,
            "integrations": [{"lane": "next", "worktree": str(wt),
                              "status": "testing",
                              "gates": self._gates(
                                  head or self.x_head,
                                  fp or self.x_fp)}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
        }
        if origin_m2 is not None:
            manifest["origin_m2"] = origin_m2
        return manifest

    def _standalone(self, hold=True):
        m = self._manifest(
            self.y_bid,
            [self._primary("expo", "58.0.17", self.y_run, self.y_wt,
                           self.y_head, self.y_fp)],
            self.y_wt, head=self.y_head, fp=self.y_fp)
        batches.import_batch(m)
        if hold:
            batches.hold_batch(self.y_bid, True, "root", "test hold",
                               refresh=False)
        return batches.load_batch(self.y_bid)

    def _joint_manifest(self, bid=None, extra_primaries=()):
        primaries = [self._primary("jest", "30.5.2", self.x_run,
                                   self.x_wt, self.x_head, self.x_fp),
                     self._primary("expo", "58.0.17", self.y_run,
                                   self.y_wt, self.y_head, self.y_fp)]
        primaries.extend(extra_primaries)
        return self._manifest(bid or self.joint_bid, primaries,
                              self.x_wt, origin_m2=self.joint_op)


class JointExemptTests(JointExemptFixture):
    def test_held_standalone_exempted(self):
        self._standalone(hold=True)
        rec = batches.import_batch(self._joint_manifest())
        self.assertEqual(len(rec["primaries"]), 2)
        self.assertEqual(rec.get("origin_m2"), self.joint_op)

    def test_unheld_standalone_refused(self):
        self._standalone(hold=False)
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self._joint_manifest())
        self.assertIn("보류", str(ctx.exception))
        self.assertFalse(batches.batch_file(self.joint_bid).exists())

    def test_unrelated_duplicate_still_refused(self):
        self._standalone(hold=True)
        run = store.create("fakerepo", "leftpad@1.0.0", "t", None,
                           {"by": "t"})
        z_run = run["id"]
        st = store.load(z_run)
        st["dep"] = "leftpad"
        st["target"] = "1.0.0"
        store.save(st)
        _bind_accept(z_run, self.x_head, self.x_fp)
        qst = sched.load("fakerepo")
        qst.setdefault("queue", []).append(
            {"key": "kz", "repo": "fakerepo", "dep": "leftpad",
             "target": "1.0.0", "lane": "next", "status": "waiting-review",
             "run_id": z_run, "branch": "codex/run-1"})
        sched.save(qst)
        other = self._manifest(
            "auto-other-1",
            [self._primary("leftpad", "1.0.0", z_run, self.x_wt,
                           self.x_head, self.x_fp,
                           branch="codex/run-1")],
            self.x_wt)
        batches.import_batch(other)
        dup = self._primary("leftpad", "1.0.0", z_run, self.x_wt,
                            self.x_head, self.x_fp,
                            branch="codex/run-1")
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self._joint_manifest(extra_primaries=[dup]))
        self.assertIn("duplicates candidate", str(ctx.exception))
        self.assertIn("leftpad", str(ctx.exception))

    def test_blocked_joint_no_exemption(self):
        self._standalone(hold=True)
        rec = json.loads(batches.op_file(self.joint_op).read_text(
            encoding="utf-8"))
        rec["status"] = "blocked"
        write_json(batches.op_file(self.joint_op), rec)
        with self.assertRaises(batches.BatchError) as ctx:
            batches.import_batch(self._joint_manifest())
        self.assertIn("duplicates candidate", str(ctx.exception))

    def test_driver_stage_batches_joint_with_held_standalone(self):
        # The driver's batch stage resumes the done joint on a real
        # import: the held standalone no longer blocks it, both
        # primaries land, and no lane block is counted.
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["echo", "x"],
                                             "gate": "unit"}]}})
        self._standalone(hold=True)
        joint_rec = json.loads(batches.op_file(self.joint_op).read_text(
            encoding="utf-8"))
        extras, divert = m2._joint_extras("fakerepo", joint_rec)
        self.assertEqual(divert, "")
        self.assertEqual(len(extras), 1)
        fc = {"ok": True, "wall_s": 1,
              "steps": [{"rc": 0, "argv": ["echo", "x"],
                         "log": str(self.log)}]}
        from openmainty import fastcheck as _fc
        Path(_fc.op_fastcheck_path(
            self.joint_op,
            Path(os.environ["OPENMAINTY_HOME"]))).write_text(
                json.dumps(fc), encoding="utf-8")
        out = m2._run_batch_stage(
            "fakerepo", self.x_run, "next", joint_rec, "base-1", fc,
            extra_primaries=extras)
        self.assertTrue(out.get("ran"), out)
        rec = batches.load_batch(out["batch"]["id"])
        self.assertEqual(len(rec["primaries"]), 2)
        st = sched.load("fakerepo")
        self.assertFalse((st.get("m2_consec_blocks") or {}).get("next"))

    def test_superseded_never_ready_after_release(self):
        self._standalone(hold=True)
        batches.import_batch(self._joint_manifest())
        batches.hold_batch(self.y_bid, False, "root", refresh=False)
        brec = batches.load_batch(self.y_bid)
        live, _ = batches.live_map_with_diag([brec])
        res = batches.evaluate(brec, live)
        self.assertFalse(all(r["ready"] for r in res["results"]))
        reasons = [r for res0 in res["results"]
                   for r in res0.get("reasons") or []]
        self.assertTrue(
            any(r.startswith("함께 올림으로 대체: ") for r in reasons),
            reasons)
        jrec = batches.load_batch(self.joint_bid)
        live, _ = batches.live_map_with_diag([jrec])
        jres = batches.evaluate(jrec, live)
        jreasons = [r for res0 in jres["results"]
                    for r in res0.get("reasons") or []]
        self.assertFalse(any("함께 올림으로 대체" in r for r in jreasons),
                         jreasons)


class M217cFixture(ManifestFixture):
    """M2.17c: an old joint batch, a done refreshed joint op and
    the M2.17 supersede entry. Filenames mirror live: the old
    joint ends -joint.json, the refreshed one ends
    -joint-<dep>-<target>.json (the shape the old map glob
    missed)."""

    def setUp(self):
        super().setUp()
        # Live-shaped ids: the old joint fits in 64 chars, the
        # refreshed joint does not (M2.17g suffix path).
        self.old_op = "prep-run-1-nitro-next-eslint-pair-20261019-joint"
        self.new_op = self.old_op + "-expo-58-0-19"
        self.old_bid = m2.batch_id_for_op(self.old_op)
        self.new_bid = m2.batch_id_for_op(self.new_op)
        run = store.create("fakerepo", "expo@58.0.19", "t", None,
                           {"by": "t"})
        self.y_run = run["id"]
        st = store.load(self.y_run)
        st["dep"] = "expo"
        st["target"] = "58.0.19"
        store.save(st)
        # The stage dedupe reads the run state's dep/target (not
        # the rec's), so the member run must carry it — same as
        # the Y run above.
        xst = store.load(self.run_id)
        xst["dep"] = "jest"
        xst["target"] = "30.5.2"
        store.save(xst)
        # The old joint's member run is a different run from the
        # refreshed member (live: run-04ae94c5 vs run-23fdef89) —
        # only the partner run is shared.
        old_y = store.create("fakerepo", "expo@58.0.17", "t", None,
                             {"by": "t"})
        self.y_old_run = old_y["id"]
        ost = store.load(self.y_old_run)
        ost["dep"] = "expo"
        ost["target"] = "58.0.17"
        store.save(ost)
        self._write_joint_op(self.old_op, "58.0.17", self.y_old_run,
                             self.run_id, None,
                             "2026-10-24T06:00:00+00:00")
        self.new_rec = self._write_joint_op(
            self.new_op, "58.0.19", self.y_run, self.y_run,
            ["prep-run-1-base-1"], "2026-10-24T07:00:00+00:00")
        # The partner's solo op (live: the done RN prepare the
        # refresh derived from) — the stage binds it inline.
        write_json(batches.op_file("prep-run-1-base-1"), {
            "op": "prepare", "op_id": "prep-run-1-base-1",
            "run_id": self.run_id, "repo": "fakerepo", "lane": "next",
            "batch_id": "base-1", "dep": "jest", "target": "30.5.2",
            "status": "done"})
        self._write_old_batch()

    def _write_joint_op(self, op_id, y_target, y_run_id, run_id,
                        joint_with, updated_at):
        rec = {
            "op": "reresolve", "op_id": op_id, "kind": "joint",
            "status": "done", "repo": "fakerepo", "lane": "next",
            "run_id": run_id, "runs": [self.run_id, y_run_id],
            "batch_id": "base-1", "base": {"batch": "base-1"},
            "dep": "expo", "target": y_target,
            "joint_with": joint_with or [],
            "joint_candidates": [
                {"run": self.run_id, "dep": "jest",
                 "target": "30.5.2",
                 "head": self.rec["result"]["head"]},
                {"run": y_run_id, "dep": "expo",
                 "target": y_target,
                 "head": self.rec["result"]["head"]}],
            "result": {"path": self.rec["result"]["path"],
                       "branch": "codex/run-1",
                       "head": self.rec["result"]["head"],
                       "fingerprint": self.rec["result"]["fingerprint"]},
            "updated_at": updated_at}
        write_json(batches.op_file(op_id), rec)
        return rec

    def _write_old_batch(self):
        write_json(batches.batch_file(self.old_bid), {
            "id": self.old_bid, "repo": "fakerepo",
            "target_branch": "next", "purpose": "joint test",
            "origin_m2": self.old_op,
            "primaries": [
                {"dep": "jest", "target": "30.5.2", "lane": "next",
                 "run_id": self.run_id,
                 "review": {"verdict": "accept"},
                 "required_gates": ["unit"]},
                {"dep": "expo", "target": "58.0.17", "lane": "next",
                 "run_id": self.y_old_run,
                 "review": {"verdict": "accept"},
                 "required_gates": ["unit"]}],
            "integrations": [{"lane": "next", "worktree": "/none",
                              "status": "done",
                              "gates": [{"key": "unit", "status": "pass",
                                         "evidence": [
                                             {"label": "log",
                                              "kind": "file",
                                              "origin": str(self.log)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
            "inherited": [],
            "updated_at": "2026-10-24T06:00:00+00:00"})
        batches.hold_batch(self.old_bid, True, "root", "test hold",
                           refresh=False)

    def policy_on(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)

    def stage(self, rec, seen):
        def fake_import(manifest):
            seen.append(manifest)
            return {"id": manifest["id"]}
        return m2._run_batch_stage(
            "fakerepo", self.run_id, "next", rec, "base-1",
            self.fc, _import=fake_import)


class M217cTests(M217cFixture):
    def test_map_marks_old_replaced_by_new(self):
        # The M2.17 entry exists only when the scan sees the
        # refreshed -joint-<dep>-<target> file (M2.17c glob fix).
        # The M2.14b half also maps the solo partner op the
        # refresh derived from.
        self.assertEqual(
            batches._joint_supersede_map(),
            {self.old_bid: self.new_op,
             m2.batch_id_for_op("prep-run-1-base-1"): self.new_op})

    def test_refreshed_joint_batches(self):
        self.policy_on()
        seen = []
        out = self.stage(self.new_rec, seen)
        self.assertTrue(out.get("ran"), out)
        self.assertEqual(out["batch"]["id"], self.new_bid)
        self.assertEqual(seen[0]["origin_m2"], self.new_op)
        st = sched.load("fakerepo")
        self.assertFalse((st.get("m2_consec_blocks") or {}).get("next"))

    def test_without_supersede_entry_still_skipped(self):
        # Same member key, but the op is not the recorded
        # replacement: today's skip, naming the holding batch.
        self.policy_on()
        other = dict(self.new_rec,
                     op_id="prep-run-9-base-9-joint-expo-59-0-0")
        seen = []
        out = self.stage(other, seen)
        self.assertFalse(out.get("ran"))
        self.assertEqual(out.get("reason"), f"dup of {self.old_bid}")
        self.assertEqual(seen, [])

    def test_helper_narrowness(self):
        self.assertTrue(m2._refreshed_joint_exempts(
            "fakerepo", self.new_rec, self.old_bid))
        self.assertFalse(m2._refreshed_joint_exempts(
            "fakerepo", self.rec, self.old_bid))
        blocked = dict(self.new_rec, status="blocked")
        self.assertFalse(m2._refreshed_joint_exempts(
            "fakerepo", blocked, self.old_bid))
        self.assertFalse(m2._refreshed_joint_exempts(
            "fakerepo", self.new_rec, "auto-no-such-batch"))
        other_repo = dict(self.new_rec, repo="other")
        self.assertFalse(m2._refreshed_joint_exempts(
            "fakerepo", other_repo, self.old_bid))

    def test_old_batch_never_ready(self):
        brec = batches.load_batch(self.old_bid)
        live, _ = batches.live_map_with_diag([brec])
        res = batches.evaluate(brec, live)
        self.assertFalse(all(r["ready"] for r in res["results"]))
        reasons = [r for res0 in res["results"]
                   for r in res0.get("reasons") or []]
        self.assertTrue(
            any(r.startswith("함께 올림으로 대체: ") for r in reasons),
            reasons)
        # Releasing the hold changes nothing: the superseded old
        # joint is never READY (the M2.14c rule).
        batches.hold_batch(self.old_bid, False, "root", refresh=False)
        brec = batches.load_batch(self.old_bid)
        live, _ = batches.live_map_with_diag([brec])
        res = batches.evaluate(brec, live)
        self.assertFalse(all(r["ready"] for r in res["results"]))
        reasons = [r for res0 in res["results"]
                   for r in res0.get("reasons") or []]
        self.assertTrue(
            any(r == f"함께 올림으로 대체: {self.new_op}"
                for r in reasons),
            reasons)

    def test_third_tick_no_rebatch(self):
        # The dup check runs before the existing-id check, so a
        # real third tick reports the new batch as the holder. It
        # is not a map key, so no exemption misfires — same op or
        # a later op on the same member key.
        self.policy_on()
        # Registered after the old hold, so strictly newer (computed,
        # never hand-typed): the newest holder wins the dup check.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(
            timespec="microseconds")
        write_json(batches.batch_file(self.new_bid), {
            "id": self.new_bid, "repo": "fakerepo",
            "target_branch": "next", "origin_m2": self.new_op,
            "primaries": [{"dep": "jest", "target": "30.5.2",
                           "lane": "next"}],
            "integrations": [], "policy": {},
            "updated_at": now})
        seen = []
        out = self.stage(self.new_rec, seen)
        self.assertFalse(out.get("ran"))
        self.assertEqual(out.get("reason"), f"dup of {self.new_bid}")
        self.assertEqual(seen, [])
        other = dict(self.new_rec,
                     op_id="prep-run-9-base-9-joint-expo-59-0-0")
        out = self.stage(other, seen)
        self.assertFalse(out.get("ran"))
        self.assertEqual(out.get("reason"), f"dup of {self.new_bid}")
        self.assertEqual(seen, [])


class M217dTickFixture(M217cFixture):
    """M2.17d: the M2.17c joint pair as two verified tick members
    with real worktrees, so the tick batches for real."""

    def setUp(self):
        super().setUp()
        self.prepared = []
        self.co = Path(self.tmp.name) / "checkout"
        y_wt = store.run_dir(self.y_run) / "work"
        y_wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", str(y_wt),
                        "-b", "codex/run-y"],
                       cwd=str(self.co), check=True,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        snap = runner.source_snapshot(y_wt)
        st = store.load(self.y_run)
        st["workCommit"] = snap["head"]
        st["workTreeFingerprint"] = snap["fingerprint"]
        store.save(st)
        _bind_accept(self.y_run, snap["head"], snap["fingerprint"])
        _bind_accept(self.run_id, self.rec["result"]["head"],
                     self.rec["result"]["fingerprint"])
        # The stage builds its worker gate from the repos profile.
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["echo", "x"],
                                             "gate": "unit"}]}})
        # Passing receipt for the refreshed joint op.
        from openmainty import fastcheck as _fc
        Path(_fc.op_fastcheck_path(
            self.new_op,
            Path(os.environ["OPENMAINTY_HOME"]))).write_text(
                json.dumps({"ok": True, "wall_s": 1, "steps":
                            [{"rc": 0, "argv": ["echo", "x"],
                              "log": str(self.log)}]}),
                encoding="utf-8")
        self._write_queue("xy")

    def _write_queue(self, order):
        # The tick sorts by (created_at, run_id): the order
        # argument controls who runs first, not the list order.
        if order == "xy":
            first, second = "06:10:00", "06:11:00"
        else:
            first, second = "06:11:00", "06:10:00"
        x = (self.run_id, "jest", "30.5.2", "codex/run-1",
             f"2026-10-24T{first}+00:00")
        y = (self.y_run, "expo", "58.0.19", "codex/run-y",
             f"2026-10-24T{second}+00:00")
        seq = (x, y)
        st = sched.load("fakerepo")
        st["queue"] = [
            {"key": f"k-{r}", "repo": "fakerepo", "dep": d,
             "target": t, "lane": "next", "status": "waiting-review",
             "verified": True, "run_id": r, "branch": b,
             "created_at": c}
            for (r, d, t, b, c) in seq]
        sched.save(st)

    def _no_prepare(self, run_id, base_id, origin="m2"):
        self.prepared.append(run_id)
        raise AssertionError(f"unexpected prepare {run_id}")

    def tick(self):
        return m2.tick_repo("fakerepo", _running="promoted",
                            _prepare=self._no_prepare)

    def _blocked_events(self):
        return [e for e in events.list_events("fakerepo")
                if e.get("kind") == "m2-blocked"]

    def _titles(self):
        return [str(e.get("title") or "")
                for e in events.list_events("fakerepo")]


class M217dTickTests(M217dTickFixture):
    def _mock_import(self, calls):
        def fake(manifest, allowed_roots=None):
            calls.append(manifest)
            return {"id": manifest["id"]}
        return mock.patch.object(batches, "import_batch", fake)

    def _assert_single_batch(self, out, carried, calls):
        # The stage reached the real manifest builder exactly once
        # (import itself is mocked: the M2.17c stage decision plus
        # the tick wiring are under test, not the import gate).
        self.assertEqual(self.prepared, [])
        self.assertTrue(out["ran"], out)
        self.assertEqual(len(out["batched"]), 1)
        self.assertEqual(out["batched"][0]["run"], self.y_run)
        self.assertEqual(out["batched"][0]["batch"]["id"],
                         self.new_bid)
        self.assertEqual(len(calls), 1)
        manifest = calls[0]
        self.assertEqual(manifest["id"], self.new_bid)
        self.assertEqual(manifest.get("origin_m2"), self.new_op)
        primaries = manifest.get("primaries") or []
        self.assertEqual(len(primaries), 2)
        by_dep = {p.get("dep"): p for p in primaries}
        # Whichever member item carried the tick, the manifest
        # binds the fresh member run to the new target and the
        # partner run to its own — never misbound, never doubled.
        self.assertEqual(by_dep["expo"]["target"], "58.0.19")
        self.assertEqual(by_dep["expo"]["run_id"], self.y_run)
        self.assertEqual(by_dep["jest"]["target"], "30.5.2")
        self.assertEqual(by_dep["jest"]["run_id"], self.run_id)
        carried_skips = [s for s in out["skipped"]
                         if s.get("run") == carried]
        self.assertEqual(len(carried_skips), 1)
        self.assertIn("이미 실행 중", carried_skips[0]["reason"])
        self.assertEqual(self._blocked_events(), [])

    def test_tick_batches_refreshed_joint_x_first(self):
        # Live order: the dup member's item runs first.
        self.policy_on()
        self._write_queue("xy")
        calls = []
        with self._mock_import(calls):
            out = self.tick()
        self._assert_single_batch(out, self.y_run, calls)
        dup_x = [s for s in out["skipped"]
                 if s.get("run") == self.run_id]
        self.assertEqual(dup_x, [])
        # The old joint batch stays never-READY with the note.
        brec = batches.load_batch(self.old_bid)
        live, _ = batches.live_map_with_diag([brec])
        res = batches.evaluate(brec, live)
        self.assertFalse(all(r["ready"] for r in res["results"]))
        reasons = [r for res0 in res["results"]
                   for r in res0.get("reasons") or []]
        self.assertTrue(
            any(r == f"함께 올림으로 대체: {self.new_op}"
                for r in reasons),
            reasons)

    def test_tick_batches_refreshed_joint_y_first(self):
        self.policy_on()
        self._write_queue("yx")
        calls = []
        with self._mock_import(calls):
            out = self.tick()
        self._assert_single_batch(out, self.run_id, calls)

    def test_dup_skip_without_exemption_does_not_starve(self):
        # No map entry (different base): the dup member skips
        # without claiming, so the partner is never told the op
        # is "already running". The stage pre-check then skips
        # the stale member key with the dup note — no import call,
        # no block, no cooldown count.
        self.policy_on()
        opf = batches.op_file(self.new_op)
        rec = json.loads(opf.read_text(encoding="utf-8"))
        rec["base"] = {"batch": "base-9"}
        write_json(opf, rec)
        # The M2.17 old->new entry is gone (only the M2.14b solo
        # entry stays): no exemption for the old batch.
        self.assertNotIn(self.old_bid,
                         batches._joint_supersede_map())
        self._write_queue("xy")
        calls = []
        with self._mock_import(calls):
            out = self.tick()
        self.assertEqual(self.prepared, [])
        self.assertEqual(out["batched"], [])
        self.assertEqual(calls, [])
        dup_x = [s for s in out["skipped"]
                 if s.get("run") == self.run_id]
        self.assertEqual(len(dup_x), 1)
        self.assertEqual(dup_x[0]["reason"],
                         f"dup of {self.old_bid}")
        carried_y = [s for s in out["skipped"]
                     if s.get("run") == self.y_run
                     and "이미 실행 중" in str(s.get("reason") or "")]
        self.assertEqual(carried_y, [])
        self.assertTrue(
            any("자율 묶음 건너뜀" in t and self.old_bid in t
                for t in self._titles()),
            self._titles())
        self.assertEqual(self._blocked_events(), [])
        st = sched.load("fakerepo")
        self.assertFalse((st.get("m2_consec_blocks") or {}).get("next"))

    def test_second_tick_no_rebatch(self):
        self.policy_on()
        self._write_queue("xy")
        calls = []
        with self._mock_import(calls):
            out = self.tick()
        self.assertTrue(out["ran"], out)
        self.assertEqual(len(calls), 1)
        # The mock writes nothing: persist what a real import
        # would, so the second tick sees the batch and stops.
        write_json(batches.batch_file(self.new_bid), {
            "id": self.new_bid, "repo": "fakerepo",
            "target_branch": "next", "origin_m2": self.new_op,
            "primaries": [{"dep": "jest", "target": "30.5.2",
                           "lane": "next",
                           "run_id": self.run_id}],
            "integrations": [], "policy": {},
            "updated_at": calls[0].get("updated_at") or
            "2026-10-24T07:00:00+00:00"})
        calls2 = []
        with self._mock_import(calls2):
            out2 = self.tick()
        self.assertEqual(out2["batched"], [])
        self.assertEqual(self._blocked_events(), [])
        self.assertEqual(calls2, [])
        reasons = [str(s.get("reason") or "")
                   for s in out2["skipped"]]
        self.assertTrue(any("이미 기록된 작업 있음" in r
                            for r in reasons),
                        reasons)
        self.assertFalse(any(r.startswith("dup of ")
                             for r in reasons),
                         reasons)

    def _assert_real_batch(self, out, carried):
        # No mock: the real import_batch writes the batch file.
        self.assertEqual(self.prepared, [])
        self.assertTrue(out["ran"], out)
        self.assertEqual(len(out["batched"]), 1)
        self.assertEqual(out["batched"][0]["run"], self.y_run)
        self.assertEqual(out["batched"][0]["batch"]["id"],
                         self.new_bid)
        rec = batches.load_batch(self.new_bid)
        self.assertEqual(rec.get("origin_m2"), self.new_op)
        primaries = rec.get("primaries") or []
        self.assertEqual(len(primaries), 2)
        by_dep = {p.get("dep"): p for p in primaries}
        self.assertEqual(by_dep["expo"]["target"], "58.0.19")
        self.assertEqual(by_dep["expo"]["run_id"], self.y_run)
        self.assertEqual(by_dep["jest"]["target"], "30.5.2")
        self.assertEqual(by_dep["jest"]["run_id"], self.run_id)
        carried_skips = [s for s in out["skipped"]
                         if s.get("run") == carried]
        self.assertEqual(len(carried_skips), 1)
        self.assertIn("이미 실행 중", carried_skips[0]["reason"])
        self.assertEqual(self._blocked_events(), [])

    def test_tick_real_import_registers_joint_x_first(self):
        # The live shape: the old joint (held) holds the RN key
        # and run; nothing holds the expo key.
        self.policy_on()
        self._write_queue("xy")
        out = self.tick()
        self._assert_real_batch(out, self.y_run)
        dup_x = [s for s in out["skipped"]
                 if s.get("run") == self.run_id]
        self.assertEqual(dup_x, [])
        # The old joint batch stays held, never READY, with the
        # supersede note.
        brec = batches.load_batch(self.old_bid)
        self.assertTrue(bool((brec.get("hold") or {}).get("by")))
        live, _ = batches.live_map_with_diag([brec])
        res = batches.evaluate(brec, live)
        self.assertFalse(all(r["ready"] for r in res["results"]))
        reasons = [r for res0 in res["results"]
                   for r in res0.get("reasons") or []]
        self.assertTrue(
            any(r == f"함께 올림으로 대체: {self.new_op}"
                for r in reasons),
            reasons)

    def test_tick_real_import_registers_joint_y_first(self):
        self.policy_on()
        self._write_queue("yx")
        out = self.tick()
        self._assert_real_batch(out, self.run_id)

    def test_tick_unheld_old_joint_refused(self):
        # The import gate still requires the hold: released, the
        # superseded old joint refuses with today's text, and the
        # stage records the block.
        self.policy_on()
        batches.hold_batch(self.old_bid, False, "root", refresh=False)
        self._write_queue("xy")
        out = self.tick()
        self.assertEqual(self.prepared, [])
        self.assertEqual(out["batched"], [])
        self.assertFalse(batches.batch_file(self.new_bid).exists())
        blocked = [
            e for e in self._blocked_events()
            if str(e.get("detail") or "").startswith(self.y_run)]
        self.assertEqual(len(blocked), 1)
        self.assertIn("먼저 보류하세요", blocked[0]["detail"])
        st = sched.load("fakerepo")
        self.assertEqual(
            (st.get("m2_consec_blocks") or {}).get("next"), 1)

    def test_tick_non_superseded_clash_skips(self):
        # Another batch holds the expo member key with no
        # supersede entry: the pre-check skips with its dup note —
        # no import call, no block, no cooldown count.
        self.policy_on()
        write_json(batches.batch_file("auto-other-1"), {
            "id": "auto-other-1", "repo": "fakerepo",
            "target_branch": "next", "origin_m2": "prep-other-1",
            "primaries": [{"dep": "expo", "target": "58.0.19",
                           "lane": "next", "run_id": "run-other"}],
            "integrations": [], "policy": {},
            "updated_at": "2026-10-24T07:30:00+00:00"})
        self.assertEqual(
            batches.joint_candidate_clash(
                "fakerepo", "next", self.new_rec), "auto-other-1")
        self._write_queue("xy")
        out = self.tick()
        self.assertEqual(self.prepared, [])
        self.assertEqual(out["batched"], [])
        self.assertFalse(batches.batch_file(self.new_bid).exists())
        self.assertTrue(
            any("자율 묶음 건너뜀" in t and "auto-other-1" in t
                for t in self._titles()),
            self._titles())
        self.assertEqual(self._blocked_events(), [])
        st = sched.load("fakerepo")
        self.assertFalse((st.get("m2_consec_blocks") or {}).get("next"))


if __name__ == "__main__":
    unittest.main()
