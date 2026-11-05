"""M2.1b-1 driver tests: auto-prepare from the daemon tick.

All offline: a temp OPENMAINTY_HOME, an injected guard sha (promoted)
or dev default, a fake prepare, and fixture batch cards for the
server-side default-base port. Py3.9 portable.
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, events, m2, repos, sched  # noqa: E402
from openmainty.batches import IntegrationError  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def card(cid, lane, ready, primaries, inherited, updated_at,
         repo="fakerepo"):
    return {"id": cid, "repo": repo, "purpose": cid, "ready": ready,
            "primaries": primaries, "attention": [],
            "inherited": [{"batch": b} for b in inherited],
            "integrations": [{"lane": lane}],
            "updated_at": updated_at}


class DriverFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        self._patched = []
        for mod, name in ((batches, "default_base"),
                          (batches, "latest_integration_op"),
                          (batches, "candidate_proof_error")):
            self._patched.append((mod, name, getattr(mod, name)))
        batches.latest_integration_op = lambda run_id: None
        batches.candidate_proof_error = lambda run_id: None

    def tearDown(self):
        for mod, name, real in self._patched:
            setattr(mod, name, real)

    def set_queue(self, items):
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["queue"] = items
            sched.save(st)

    def set_state(self, **kw):
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st.update(kw)
            sched.save(st)

    def waiting(self, run="run-m2a", lane="next", verified=True,
                created_at="2026-10-21T00:00:00+00:00", kind=None,
                nodep=False):
        item = {"run_id": run, "status": "waiting-review", "lane": lane,
                "dep": "left-hook", "target": "2.2.2",
                "verified": verified, "created_at": created_at}
        if nodep:
            item.pop("dep")
            item.pop("target")
        if kind is not None:
            item["kind"] = kind
        return item

    def real_proof(self):
        for mod, name, real in self._patched:
            if name == "candidate_proof_error":
                batches.candidate_proof_error = real

    def home_files(self):
        return sorted(str(p.relative_to(self.home))
                      for p in self.home.rglob("*") if p.is_file())

    def titles(self):
        return [e.get("title") for e in events.list_events("fakerepo")]

    def hold_lease_from_thread(self):
        acquired = threading.Event()
        release = threading.Event()
        box = {}

        def run():
            tok = sched.acquire_worker_lease("test-holder")
            box["tok"] = tok
            acquired.set()
            release.wait(15)
            if tok:
                sched.release_worker_lease(tok)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(acquired.wait(10))
        self.assertIsNotNone(box["tok"])
        self.addCleanup(lambda: (release.set(), t.join(10)))


class InertTests(DriverFixture):
    def test_1_all_off_zero_writes_zero_events(self):
        # Root's tick-window gate: with every toggle off the driver is
        # a read-only no-op — before any guard call. _running is set
        # to a promoted sha to prove the guard result is irrelevant.
        before_files = self.home_files()
        before_events = self.titles()
        out = m2.tick_repo("fakerepo", _running="promoted-sha")
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "다음 단계 꺼짐")
        self.assertEqual(self.home_files(), before_files)
        self.assertEqual(self.titles(), before_events)

    def test_2_guard_dev_inert_with_toggle_on(self):
        m2.set_policy("fakerepo", "prepare", True)
        calls = []
        out = m2.tick_repo(
            "fakerepo",
            _prepare=lambda run, base, origin="m2": calls.append(
                (run, base, origin)))
        self.assertFalse(out["ran"])
        self.assertIn("검토된 앱 사본", out["reason"])
        self.assertEqual(calls, [])
        self.assertEqual(len(self.titles()), 2)  # flip + guard wait
        out2 = m2.tick_repo("fakerepo")
        self.assertFalse(out2["ran"])
        # Steady-state guard refusal records nothing new.
        self.assertEqual(len(self.titles()), 2)


class PrepareFlowTests(DriverFixture):
    def fake_prepare(self, box, rec=None, error=None, lease_held=None):
        def run(run_id, base_id, origin="m2"):
            box.append((run_id, base_id, origin))
            if lease_held is not None:
                lease_held.append(sched.worker_lease_held())
            if error is not None:
                raise error
            got = dict(rec or {})
            got.setdefault("dep", "left-hook")
            got.setdefault("target", "2.2.2")
            # Real prepare records always carry a status (D1); the
            # fake must match that contract.
            got.setdefault("status", "done")
            got.setdefault("result", {"branch": "codex/m2a",
                                      "head": "abc123"})
            return got
        return run

    def test_3_prepare_runs_with_origin_m2_under_lease(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                   "kin": 2, "tie": 1,
                                                   "via_ancestor": False}
        box, held = [], []
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self.fake_prepare(box, lease_held=held))
        self.assertTrue(out["ran"])
        self.assertEqual(box, [("run-m2a", "base-1", "m2")])
        self.assertEqual(held, [True])
        self.assertEqual(out["prepared"][0]["branch"], "codex/m2a")
        # Prepare-only stops after the op with the next-stage event.
        self.assertIn("자율 다음 단계 꺼짐", self.titles())
        # The lease is released afterwards.
        self.assertFalse(sched.worker_lease_held())

    def test_4_recorded_op_never_retried(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.latest_integration_op = lambda run_id: {"status": "blocked"}
        box = []
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self.fake_prepare(box))
        self.assertFalse(out["ran"])
        self.assertEqual(box, [])
        self.assertEqual(out["skipped"][0]["reason"], "이미 기록된 작업 있음")
        self.assertNotIn("자율 준비 막힘", " ".join(self.titles()))

    def test_5_unverified_candidate_skips_without_block(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                   "kin": 0, "tie": 1,
                                                   "via_ancestor": False}
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self.fake_prepare(
                [], error=IntegrationError("candidate review proof "
                                           "not fresh (review=stale)")))
        self.assertFalse(out["ran"])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertNotIn("m2-blocked", kinds)

    def test_6_outside_branch_blocked(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                   "kin": 0, "tie": 1,
                                                   "via_ancestor": False}
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self.fake_prepare(
                [], rec={"status": "done",
                         "result": {"branch": "main",
                                    "head": "x"}}))
        self.assertFalse(out["ran"])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-blocked", kinds)


class BlockedBase(DriverFixture):
    """Shared blocked-rec helpers (no tests here)."""

    @staticmethod
    def prepare_with(rec):
        def fake(run_id, base_id, origin="m2"):
            return dict(rec)
        return fake

    def make_repo(self):
        co = Path(self.tmp.name) / "co"
        co.mkdir()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        for args in (["git", "init", "-q", "."],
                     ["git", "config", "user.email", "t@t"],
                     ["git", "config", "user.name", "t"],
                     ["git", "config", "commit.gpgsign", "false"]):
            proc = subprocess.run(args, cwd=str(co), env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, args)
        (co / "f.txt").write_text("hi\n", encoding="utf-8")
        for args in (["git", "add", "f.txt"],
                     ["git", "commit", "-qm", "init"]):
            proc = subprocess.run(args, cwd=str(co), env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, args)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(co),
                              capture_output=True, text=True,
                              timeout=60).stdout.strip()
        return co, head, env

    def base(self):
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}

    def blocked_rec(self, run="run-m2a", error="cherry-pick conflicted",
                    branch="codex/m2a", head="", paths=None, origin="m2",
                    status="blocked"):
        return {"op_id": "prep-%s-base-1" % run, "origin": origin,
                "status": status, "dep": "d", "target": "t",
                "error": error,
                "result": {"branch": branch, "head": head,
                           "path": "/nonexistent-wt",
                           "conflicted_paths": list(paths or [])}}

    def kinds(self):
        return [e.get("kind") for e in events.list_events("fakerepo")]


class BlockedPrepareTests(BlockedBase):
    """D1-D4: a returned blocked record is a block, never a done."""

    def test_d1_blocked_record_blocks_without_done(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        rec = self.blocked_rec(
            error="cherry-pick conflicted or failed: abc",
            paths=["package.json", "yarn.lock"])
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": rec)
        self.assertFalse(out["ran"])
        self.assertNotIn("m2-done", self.kinds())
        self.assertIn("m2-blocked", self.kinds())
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-blocked"]
        self.assertTrue(any("package.json" in (d or "")
                            for d in details))
        st = sched.load("fakerepo")
        self.assertEqual((st.get("m2_consec_blocks") or {}).get("next"),
                         1)

    def test_d1_empty_head_blocks(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        rec = {"op_id": "prep-run-m2a-base-1", "status": "done",
               "dep": "d", "target": "t",
               "result": {"branch": "codex/m2a", "head": ""}}
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": rec)
        self.assertFalse(out["ran"])
        self.assertNotIn("m2-done", self.kinds())
        self.assertIn("m2-blocked", self.kinds())
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-blocked"]
        self.assertTrue(any("예상치 못한 준비 상태" in (d or "")
                            for d in details))

    def test_d2_reconcile_emits_once_with_correction(self):
        # Tick 1: false done (the 11:22Z shape). Tick 2: the op file
        # says blocked — exactly one "정정: " event, history kept.
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        done = {"op_id": "prep-run-m2a-base-1", "status": "done",
                "dep": "d", "target": "t",
                "result": {"branch": "codex/m2a", "head": "abc"}}
        out1 = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": done)
        self.assertTrue(out1["ran"])
        blocked = self.blocked_rec()
        batches.latest_integration_op = lambda run_id: blocked
        out2 = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: self.fail("no retry"))
        self.assertFalse(out2["ran"])
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-blocked"]
        corrected = [d for d in details
                     if (d or "").startswith("정정: ")]
        self.assertEqual(len(corrected), 1)
        # The false done line is history: still present, untouched.
        self.assertIn("자율 준비 완료 d t", self.titles())
        out3 = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: self.fail("no retry"))
        details3 = [e.get("detail") for e in
                    events.list_events("fakerepo")
                    if e.get("kind") == "m2-blocked"]
        self.assertEqual(len(details3), len(details))

    def test_d3_branch_removed_only_at_base_head(self):
        from openmainty.batches import remove_leftover_branch_at_base
        co, head, env = self.make_repo()
        subprocess.run(["git", "branch", "codex/gone", head],
                       cwd=str(co), check=True, timeout=60, env=env)
        note = remove_leftover_branch_at_base(str(co), "codex/gone",
                                              head, "/nonexistent-wt")
        self.assertIn("정리됨", note)
        gone = subprocess.run(["git", "branch", "--list", "codex/gone"],
                              cwd=str(co), capture_output=True,
                              text=True, timeout=60).stdout.strip()
        self.assertEqual(gone, "")
        # A branch with its own commit survives, named honestly.
        subprocess.run(["git", "checkout", "-qb", "codex/own", head],
                       cwd=str(co), check=True, timeout=60, env=env)
        (co / "g.txt").write_text("mine\n", encoding="utf-8")
        subprocess.run(["git", "add", "g.txt"], cwd=str(co), check=True,
                       timeout=60, env=env)
        subprocess.run(["git", "commit", "-qm", "own"], cwd=str(co),
                       check=True, timeout=60, env=env)
        note2 = remove_leftover_branch_at_base(str(co), "codex/own",
                                               head, "/nonexistent-wt")
        self.assertIn("유지됨", note2)
        # A lingering worktree path also keeps the branch.
        subprocess.run(["git", "branch", "codex/stay", head],
                       cwd=str(co), check=True, timeout=60, env=env)
        note3 = remove_leftover_branch_at_base(str(co), "codex/stay",
                                               head, str(co))
        self.assertIn("유지됨", note3)

    def test_d3_driver_cleans_blocked_leftover(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        co, head, env = self.make_repo()
        subprocess.run(["git", "branch", "codex/m2a", head],
                       cwd=str(co), check=True, timeout=60, env=env)
        rec = self.blocked_rec()
        rec["base"] = {"checkout": str(co), "head": head}
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=lambda run, base, origin="m2": rec)
        gone = subprocess.run(["git", "branch", "--list", "codex/m2a"],
                              cwd=str(co), capture_output=True,
                              text=True, timeout=60).stdout.strip()
        self.assertEqual(gone, "")
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-blocked"]
        self.assertTrue(any("정리됨" in (d or "") for d in details))


class ReresolveDriverTests(BlockedBase):
    """S15(a): one automatic reresolve on lock-only/adjacent blocks."""

    def patch_reresolve(self, scope, fake):
        real_scope = batches.prepare_reresolve_scope
        real_re = batches.reresolve_on_base
        batches.prepare_reresolve_scope = lambda rec: scope
        batches.reresolve_on_base = fake
        self.addCleanup(setattr, batches, "prepare_reresolve_scope",
                        real_scope)
        self.addCleanup(setattr, batches, "reresolve_on_base", real_re)

    def lockonly_rec(self):
        rec = self.blocked_rec()
        rec["result"]["conflicted_paths"] = ["yarn.lock"]
        return rec

    def test_a_attempts_once_and_continues_marked(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        box = []
        derived = {"op_id": "prep-run-m2a-base-1-lockfix",
                   "derived_from": "prep-run-m2a-base-1",
                   "status": "done", "dep": "d", "target": "t",
                   "result": {"branch": "codex/m2a-lf",
                              "head": "derivedhead",
                              "fingerprint": "fp"}}
        self.patch_reresolve(
            {"eligible": True, "scope": "lock-only", "reason": "x"},
            lambda op_id, **k: (box.append(op_id), derived)[1])
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self.prepare_with(self.lockonly_rec()))
        self.assertTrue(out["ran"])
        self.assertEqual(box, ["prep-run-m2a-base-1"])
        self.assertEqual(out["prepared"][0]["head"], "derivedhead")
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-done", kinds)
        self.assertNotIn("m2-blocked", kinds)
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-done"]
        self.assertTrue(any("파생 잠금 출처" in (d or "")
                            for d in details))
        # Second tick: the derived done is on record — hands off, no
        # second attempt.
        batches.latest_integration_op = lambda run_id: dict(derived)
        out2 = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: self.fail("no retry"))
        self.assertFalse(out2["ran"])
        self.assertEqual(box, ["prep-run-m2a-base-1"])

    def test_rederive_done_says_reapplied_on_base(self):
        # M2.9a: a done record with kind rederive (returned straight
        # from prepare) says it was re-applied on the base — never
        # equal to the original commit.
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        derived = {"op_id": "prep-run-m2a-base-1-rederive",
                   "op": "reresolve", "kind": "rederive",
                   "derived_from": "prep-run-m2a-base-1",
                   "status": "done", "dep": "d", "target": "t",
                   "result": {"branch": "codex/m2a-rd",
                              "head": "derivedhead",
                              "fingerprint": "fp"}}
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: dict(derived))
        self.assertTrue(out["ran"])
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-done"]
        self.assertTrue(any("기준에서 다시 적용" in (d or "")
                            for d in details))
        self.assertFalse(any("파생 잠금 출처" in (d or "")
                             for d in details))

    def test_a_busy_skips_without_block(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()

        def busy(op_id, **k):
            raise batches.IntegrationError(
                "다른 확인 작업이 %s에서 실행 중입니다" % op_id, 409)

        self.patch_reresolve(
            {"eligible": True, "scope": "adjacent", "reason": "x"},
            busy)
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self.prepare_with(self.lockonly_rec()))
        self.assertFalse(out["ran"])
        self.assertEqual(out["skipped"][0]["reason"],
                         "다른 확인 작업 실행 중")
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertNotIn("m2-blocked", kinds)
        st = sched.load("fakerepo")
        self.assertFalse((st.get("m2_consec_blocks") or {}).get("next"))

    def test_a_failure_blocks_once(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()

        def boom(op_id, **k):
            raise batches.IntegrationError("regen exploded")

        self.patch_reresolve(
            {"eligible": True, "scope": "lock-only", "reason": "x"},
            boom)
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self.prepare_with(self.lockonly_rec()))
        self.assertFalse(out["ran"])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-blocked", kinds)
        st = sched.load("fakerepo")
        self.assertEqual((st.get("m2_consec_blocks") or {}).get("next"),
                         1)

    def test_c_single_blocked_event(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=self.prepare_with(self.blocked_rec()))
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertEqual(kinds.count("m2-blocked"), 1)


class RefreshDriverTests(BlockedBase):
    """S15(b): stale same-line blocks queue one refresh run."""

    def patch_scope(self, scope):
        real = batches.prepare_reresolve_scope
        batches.prepare_reresolve_scope = lambda rec: scope
        self.addCleanup(setattr, batches, "prepare_reresolve_scope",
                        real)

    def stale_rec(self):
        rec = self.blocked_rec(
            error="기준이 같은 의존성 선언을 이미 변경함(^2) — 새로",
            paths=["package.json", "yarn.lock"])
        return rec

    def queued_refreshes(self):
        return [i for i in sched.load("fakerepo").get("queue")
                if isinstance(i, dict)
                and i.get("origin") == "m2-refresh"]

    def test_b_sameline_queues_unverified_refresh(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "refresh", True)
        self.set_queue([self.waiting()])
        self.base()
        self.patch_scope({"eligible": False, "scope": "same-line",
                          "reason": "stale"})
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self.prepare_with(self.stale_rec()))
        self.assertFalse(out["ran"])
        items = self.queued_refreshes()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["dep"], "d")
        self.assertEqual(item["target"], "t")
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["supersedes"], "run-m2a")
        self.assertNotIn("run_id", item)
        self.assertNotIn("verified", item)
        self.assertIn("m2-refresh",
                      [e.get("kind") for e in
                       events.list_events("fakerepo")])
        # Exempt: queued, so no cooldown count.
        st = sched.load("fakerepo")
        self.assertFalse((st.get("m2_consec_blocks") or {}).get("next"))
        # Second tick reconciles the same stale op: still one item,
        # still no count (progress pending).
        batches.latest_integration_op = lambda run_id: self.stale_rec()
        out2 = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: self.fail("no retry"))
        self.assertEqual(len(self.queued_refreshes()), 1)
        st2 = sched.load("fakerepo")
        self.assertFalse(
            (st2.get("m2_consec_blocks") or {}).get("next"))

    def test_b_refresh_off_counts(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        self.patch_scope({"eligible": False, "scope": "same-line",
                          "reason": "stale"})
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=self.prepare_with(self.stale_rec()))
        self.assertEqual(self.queued_refreshes(), [])
        st = sched.load("fakerepo")
        self.assertEqual((st.get("m2_consec_blocks") or {}).get("next"),
                         1)

    def test_b_daily_cap(self):
        from datetime import datetime, timezone
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "refresh", True)
        self.set_queue([self.waiting()])
        self.base()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_refresh_log"] = [
                {"day": today, "at": today, "run": "old-%d" % i,
                 "item": "k-%d" % i} for i in range(4)]
            sched.save(st)
        self.patch_scope({"eligible": False, "scope": "same-line",
                          "reason": "stale"})
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=self.prepare_with(self.stale_rec()))
        self.assertEqual(self.queued_refreshes(), [])
        st = sched.load("fakerepo")
        self.assertEqual((st.get("m2_consec_blocks") or {}).get("next"),
                         1)


class ReconcileStabilityTests(BlockedBase):
    """E1: reconcile across ticks with a changing detail gives one
    event and one count; E2: the persisted note is reused."""

    def test_e1_three_ticks_one_event_one_count(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        self.base()
        co, head, env = self.make_repo()
        import subprocess
        subprocess.run(["git", "branch", "codex/m2a", head],
                       cwd=str(co), check=True, timeout=60, env=env)
        rec = self.blocked_rec(paths=["other/f.js"])
        rec["base"] = {"checkout": str(co), "head": head}
        # Tick 1: fresh block removes the branch ("정리됨").
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=self.prepare_with(rec))
        # Ticks 2-3: the stored record predates the persisted note,
        # so cleanup recomputes ("남은 브랜치 없음") — the detail
        # drifts, but the marker holds: no new event, no new count.
        batches.latest_integration_op = lambda run_id: rec
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=lambda *a, **k: self.fail("no retry"))
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=lambda *a, **k: self.fail("no retry"))
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertEqual(kinds.count("m2-blocked"), 1)
        st = sched.load("fakerepo")
        self.assertEqual((st.get("m2_consec_blocks") or {}).get("next"),
                         1)

    def test_e2_note_persists_and_is_reused(self):
        from openmainty import batches as _b
        from openmainty.util import write_json
        opf = _b.op_file("prep-e2-test")
        write_json(opf, {"op_id": "prep-e2-test", "status": "blocked",
                         "result": {"branch": "codex/e2"}})
        self.assertEqual(_b.record_branch_cleanup("prep-e2-test",
                                                  "브랜치 정리됨"),
                         "브랜치 정리됨")
        # First note wins, even against a different second note.
        self.assertEqual(_b.record_branch_cleanup("prep-e2-test",
                                                  "남은 브랜치 없음"),
                         "브랜치 정리됨")
        # The driver reuses the stored note without touching git.
        rec = {"op_id": "prep-e2-test",
               "result": {"branch": "codex/e2",
                          "branch_cleanup": "브랜치 정리됨"}}
        self.assertEqual(m2._cleanup_blocked_branch(rec),
                         "브랜치 정리됨")


class AlreadyPresentTests(DriverFixture):
    """E4: 'already present in base' is a skip with per-run memory."""

    def test_e4_present_skips_and_remembers(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}
        box = []

        def refuse(run_id, base_id, origin="m2"):
            box.append(run_id)
            raise batches.IntegrationError(
                "candidate d is already present in base batch "
                "'base-1' through inherited base-1")

        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=refuse)
        self.assertFalse(out["ran"])
        self.assertEqual(out["skipped"][0]["reason"],
                         "이미 기준 묶음에 포함")
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertNotIn("m2-blocked", kinds)
        st = sched.load("fakerepo")
        self.assertFalse((st.get("m2_consec_blocks") or {}).get("next"))
        # Tick 2: the memory skips before prepare — the slot is free
        # for other candidates.
        out2 = m2.tick_repo("fakerepo", _running="promoted",
                            _prepare=refuse)
        self.assertEqual(box, ["run-m2a"])
        self.assertEqual(out2["skipped"][0]["reason"],
                         "이미 기준 묶음에 포함")


class RefreshPendingGateTests(unittest.TestCase):
    """S15(b): the execute-off pending-refresh need-you gate (pure)."""

    def state(self, execute, items):
        return {"execute_enabled": execute, "queue": items}

    def item(self, status="queued"):
        return {"origin": "m2-refresh", "status": status,
                "dep": "expo", "target": "58.0.14",
                "key": "k+refresh-b", "created_at": "2026-10-21T00:00:00"}

    def test_pending_shows_only_when_execute_off(self):
        self.assertIsNone(batches.refresh_pending_item(
            self.state(True, [self.item()]), 0.0))
        self.assertIsNone(batches.refresh_pending_item(
            self.state(False, []), 0.0))
        got = batches.refresh_pending_item(
            self.state(False, [self.item()]), 0.0)
        self.assertEqual(got["kind"], "refresh-pending")
        self.assertIn("expo 58.0.14", got["title"])
        self.assertIn("실행이 꺼져", got["title"])
        self.assertEqual(got["why"], "사람이 실행을 켜야 대기열이 진행됩니다")


class LeaseBusyTests(DriverFixture):
    def test_7_busy_skips_then_blocks_after_45min(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                   "kin": 0, "tie": 1,
                                                   "via_ancestor": False}
        self.hold_lease_from_thread()
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=lambda *a, **k: self.fail("must wait"))
        self.assertFalse(out["ran"])
        self.assertIn("다음 틱", out["reason"])
        self.assertIsNotNone(sched.load("fakerepo")["m2_lease_busy_since"])
        # 46 minutes of continuous busy becomes a recorded block.
        self.set_state(m2_lease_busy_since=time.time() - 46 * 60)
        out2 = m2.tick_repo("fakerepo", _running="promoted",
                            _prepare=lambda *a, **k: self.fail("must wait"))
        self.assertEqual(out2["reason"], "45분 연속 작업 중")


class CooldownTests(DriverFixture):
    def test_8_three_blocks_cool_lane_down(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                   "kin": 0, "tie": 1,
                                                   "via_ancestor": False}
        self.set_state(m2_consec_blocks={"next": 2})

        def boom(run_id, base_id, origin="m2"):
            raise IntegrationError("conflict at 1/2")

        out = m2.tick_repo("fakerepo", _running="promoted", _prepare=boom)
        self.assertFalse(out["ran"])
        st = sched.load("fakerepo")
        self.assertIn("next", st[m2._COOLDOWN_KEY])
        self.assertIn("6시간 대기", " ".join(self.titles()))
        # While cooling, the lane is skipped without acting.
        out2 = m2.tick_repo("fakerepo", _running="promoted", _prepare=boom)
        # E5: the skip names the lane and its release time.
        self.assertTrue(
            out2["skipped"][0]["reason"].startswith("next 레인"))
        self.assertIn("까지 쉼", out2["skipped"][0]["reason"])

    def test_e5a_counted_cooldown_releases_on_flip(self):
        # End-to-end on the REAL path: three counted blocks through
        # the production counter open the cooldown; an owner
        # off->on flip must release it so the next tick prepares.
        # Fails while the flip clears the wrong key.
        m2.set_policy("fakerepo", "prepare", True)
        for _ in range(3):
            m2._count_block("fakerepo", "next")
        st = sched.load("fakerepo")
        self.assertIn("next", st.get(m2._COOLDOWN_KEY) or {})
        m2.set_policy("fakerepo", "prepare", False)
        m2.set_policy("fakerepo", "prepare", True)
        # E5: scoped patch with cleanup (Py3.9-safe) — a bare
        # module-level assignment would outlive a failing test.
        patcher = mock.patch.object(
            batches, "default_base",
            lambda repo, lane, **k: {"id": "base-1", "kin": 0, "tie": 1,
                                     "via_ancestor": False})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_queue([self.waiting()])
        calls = []

        def fake_prepare(run_id, base_id, origin="m2"):
            calls.append((run_id, base_id, origin))
            return {"status": "done", "op_id": "prep-e5a",
                    "dep": "left-hook", "target": "2.2.2",
                    "result": {"branch": "codex/e5a",
                               "head": "a" * 40,
                               "fingerprint": "f" * 64}}

        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=fake_prepare)
        self.assertEqual(calls, [("run-m2a", "base-1", "m2")])
        self.assertTrue(out["ran"])

    def test_e5c_cooling_lane_single_skip_no_promise(self):
        m2.set_policy("fakerepo", "prepare", True)
        for _ in range(3):
            m2._count_block("fakerepo", "next")
        self.set_queue([self.waiting()])

        def never(run_id, base_id, origin="m2"):
            raise AssertionError("cooling lane must not prepare")

        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=never)
        self.assertFalse(out["ran"])
        texts = [(e.get("title") or "") + "\n" + (e.get("detail") or "")
                 for e in events.list_events("fakerepo")]
        naming = [t for t in texts if "까지 쉼" in t]
        # Exactly one event names the lane and its release time.
        self.assertEqual(len(naming), 1)
        self.assertIn("next", naming[0])
        # ... and no stale "1건씩 준비" promise anywhere.
        self.assertFalse(any("1건씩" in t for t in texts))


class AttemptLockfixOriginTests(unittest.TestCase):
    def test_attempt_lockfix_passes_driver_origin(self):
        # S17: the driver stamps its derived ops origin "m2".
        seen = {}

        def fake(op_id, retry=False, origin=None):
            seen.update(op_id=op_id, retry=retry, origin=origin)
            return {"status": "done",
                    "result": {"head": "a" * 40}}

        with mock.patch.object(batches, "reresolve_on_base",
                               side_effect=fake):
            out = m2._attempt_lockfix("prep-x")
        self.assertEqual(seen.get("origin"), "m2")
        self.assertEqual(seen.get("op_id"), "prep-x")
        self.assertTrue(out["ok"])


class DefaultBaseTests(unittest.TestCase):
    def test_9_chain_picks_deepest(self):
        # kin counts transitive READY ancestors: leaf 2, mid 1,
        # base 0 — the unique maximum (the real main-lane shape).
        cards = [
            card("base", "next", 2, 2, [], "2026-10-19T01:00:00+00:00"),
            card("mid", "next", 1, 1, ["base"],
                 "2026-10-19T02:00:00+00:00"),
            card("leaf", "next", 1, 1, ["mid"],
                 "2026-10-19T03:00:00+00:00")]
        out = batches.default_base("fakerepo", "next", cards)
        self.assertEqual(out["id"], "leaf")
        self.assertEqual(out["tie"], 1)
        self.assertFalse(out["via_ancestor"])

    def test_10_sibling_tie_resolves_to_common_ancestor(self):
        cards = [
            card("pair", "next", 2, 2, [], "2026-10-19T01:00:00+00:00"),
            card("l1", "next", 1, 1, ["pair"],
                 "2026-10-19T02:00:00+00:00"),
            card("l2", "next", 1, 1, ["pair"],
                 "2026-10-19T03:00:00+00:00")]
        out = batches.default_base("fakerepo", "next", cards)
        self.assertEqual(out["id"], "pair")
        self.assertEqual(out["tie"], 2)
        self.assertTrue(out["via_ancestor"])

    def test_11_siblings_without_ancestor_have_no_default(self):
        cards = [
            card("l1", "next", 1, 1, ["ghost"],
                 "2026-10-19T02:00:00+00:00"),
            card("l2", "next", 1, 1, ["ghost"],
                 "2026-10-19T03:00:00+00:00")]
        out = batches.default_base("fakerepo", "next", cards)
        self.assertEqual(out["id"], "")
        self.assertEqual(out["tie"], 2)

    def test_12_lane_scoped_with_main_fallback(self):
        cards = [card("mbase", "main", 1, 1, [],
                      "2026-10-19T01:00:00+00:00")]
        self.assertEqual(
            batches.default_base("fakerepo", "main", cards)["id"], "mbase")
        # Next with no READY next batch falls back to main.
        self.assertEqual(
            batches.default_base("fakerepo", "next", cards)["id"], "mbase")
        # But a READY next batch wins over main.
        cards.append(card("nbase", "next", 1, 1, [],
                          "2026-10-19T02:00:00+00:00"))
        self.assertEqual(
            batches.default_base("fakerepo", "next", cards)["id"], "nbase")


class PredicateTests(DriverFixture):
    def test_r1_unverified_never_acts(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(verified=False)])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": box.append(run))
        self.assertFalse(out["ran"])
        self.assertEqual(box, [])
        self.assertEqual(out["skipped"][0]["reason"], "리뷰 승인 전")

    def test_r1_proof_prefilter_uses_prepare_predicate(self):
        # verified True but no recorded run: the REAL pre-flight
        # (store.verification_status + _accept_proof_gaps + live
        # match, the gate prepare_integration enforces) refuses.
        self.real_proof()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(run="run-ghost")])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": box.append(run))
        self.assertFalse(out["ran"])
        self.assertEqual(box, [])
        self.assertIn("unknown run", out["skipped"][0]["reason"])

    def test_r4_opaque_op_record_fails_closed(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])

        def boom(run_id):
            raise OSError("disk gone")

        batches.latest_integration_op = boom
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": box.append(run))
        self.assertFalse(out["ran"])
        self.assertEqual(box, [])
        self.assertEqual(out["skipped"][0]["reason"],
                         "작업 기록 확인 실패")

    def test_r5_oplock_busy_skips_without_block(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}

        def busy(run_id, base_id, origin="m2"):
            raise IntegrationError("다른 확인 작업이 op에서 실행 중입니다",
                                   409)

        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=busy)
        self.assertFalse(out["ran"])
        self.assertEqual(out["skipped"][0]["reason"],
                         "다른 확인 작업 실행 중")
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertNotIn("m2-blocked", kinds)
        self.assertEqual(sched.load("fakerepo").get("m2_consec_blocks"),
                         {})

    def test_r6_ambiguous_base_is_owner_choice(self):
        info_poor = [
            {"id": "a", "repo": "fakerepo", "ready": 1, "primaries": 1,
             "attention": [], "integrations": [{"lane": "next"}],
             "updated_at": "2026-10-19T01:00:00+00:00"},
            {"id": "b", "repo": "fakerepo", "ready": 1, "primaries": 1,
             "attention": [], "integrations": [{"lane": "next"}],
             "updated_at": "2026-10-19T02:00:00+00:00"}]
        # The UI keeps its newest guess; the driver must not.
        self.assertEqual(
            batches.default_base("fakerepo", "next",
                                 info_poor)["id"], "b")
        guessed = batches.default_base("fakerepo", "next", info_poor,
                                       ambiguous="empty")
        self.assertEqual(guessed["id"], "")
        self.assertEqual(guessed["tie"], 2)
        # And the driver turns that signal into the owner's skip.
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: dict(guessed)
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": box.append(run))
        self.assertFalse(out["ran"])
        self.assertEqual(box, [])
        self.assertEqual(out["skipped"][0]["reason"],
                         "기준 묶음 선택 필요")


class CapTests(DriverFixture):
    def base(self):
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}

    def test_r2_one_prepare_per_tick_oldest_first(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.base()
        self.set_queue([self.waiting(run="run-m2%d" % i,
                                     created_at="2026-10-21T00:0%d:00+00:00"
                                     % i)
                        for i in range(5)])
        box = []

        def fake(run_id, base_id, origin="m2"):
            box.append(run_id)
            return {"dep": "d", "target": "t", "status": "done",
                    "result": {"branch": "codex/" + run_id, "head": "h"}}

        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=fake)
        self.assertTrue(out["ran"])
        self.assertEqual(box, ["run-m20"])
        self.assertEqual(len(out["prepared"]), 1)
        self.assertIn("자율 준비 대기 4건", self.titles())

    def test_r2_cap_key_raises_throughput(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.base()
        self.set_queue([self.waiting(run="run-m2%d" % i,
                                     created_at="2026-10-21T00:0%d:00+00:00"
                                     % i)
                        for i in range(3)])
        self.set_state(m2_max_prepare_per_tick=2)
        box = []

        def fake(run_id, base_id, origin="m2"):
            box.append(run_id)
            return {"dep": "d", "target": "t", "status": "done",
                    "result": {"branch": "codex/" + run_id, "head": "h"}}

        m2.tick_repo("fakerepo", _running="promoted", _prepare=fake)
        self.assertEqual(box, ["run-m20", "run-m21"])


class FastcheckStageTests(DriverFixture):
    def profile_with_steps(self):
        tools = Path(self.tmp.name) / "tools"
        tools.mkdir(exist_ok=True)
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["echo", "x"],
                                              "gate": "unit"}],
                                  "toolchain": [str(tools)]}})

    def prep_rec(self, run="run-m2a"):
        return {"op_id": "prep-%s-base-1" % run, "dep": "d",
                "target": "t", "status": "done",
                "result": {"branch": "codex/%s" % run, "path": "/w",
                           "head": "abc", "fingerprint": "fp"}}

    def test_stage_open_and_runs_after_prepare(self):
        self.assertTrue(m2.STAGE_OPEN["fastcheck"])
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.profile_with_steps()
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}
        calls = []

        def fake_prepare(run_id, base_id, origin="m2"):
            return self.prep_rec(run_id)

        def fake_run(op_id, worktree, profile, head, fp):
            calls.append((op_id, worktree, head, fp))
            self.assertEqual(profile["steps"],
                             [{"argv": ["echo", "x"], "gate": "unit"}])
            return {"ok": True, "steps": [{}, {}], "failed": None}

        from openmainty import fastcheck as _fc
        real_run = _fc.run
        _fc.run = fake_run
        try:
            out = m2.tick_repo("fakerepo", _running="promoted",
                               _prepare=fake_prepare)
        finally:
            _fc.run = real_run
        self.assertTrue(out["ran"])
        self.assertEqual(calls,
                         [("prep-run-m2a-base-1", "/w", "abc", "fp")])
        self.assertIn("자율 검증 완료", self.titles())

    def test_red_step_blocks_lane_with_tail(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.profile_with_steps()
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}

        def fake_prepare(run_id, base_id, origin="m2"):
            return self.prep_rec(run_id)

        def fake_run(op_id, worktree, profile, head, fp):
            return {"ok": False,
                    "failed": {"argv": ["yarn", "jest"], "rc": 1,
                               "log_tail": "red-tail-123"}}

        from openmainty import fastcheck as _fc
        real_run = _fc.run
        _fc.run = fake_run
        try:
            out = m2.tick_repo("fakerepo", _running="promoted",
                               _prepare=fake_prepare)
        finally:
            _fc.run = real_run
        # The prepare itself ran; the red fastcheck blocks the lane.
        self.assertTrue(out["ran"])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-blocked", kinds)
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-blocked"]
        self.assertTrue(any("red-tail-123" in (d or "")
                            for d in details))

    def test_skip_never_blocks(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.profile_with_steps()
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}

        def fake_prepare(run_id, base_id, origin="m2"):
            return self.prep_rec(run_id)

        def fake_run(op_id, worktree, profile, head, fp):
            from openmainty.fastcheck import FastcheckSkip
            raise FastcheckSkip("다른 빠른 검사가 실행 중")

        from openmainty import fastcheck as _fc
        real_run = _fc.run
        _fc.run = fake_run
        try:
            out = m2.tick_repo("fakerepo", _running="promoted",
                               _prepare=fake_prepare)
        finally:
            _fc.run = real_run
        # The prepare itself ran; the skip blocks nothing.
        self.assertTrue(out["ran"])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertNotIn("m2-blocked", kinds)

    def test_missing_profile_steps_block(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.set_queue([self.waiting()])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                        "kin": 0, "tie": 1,
                                                        "via_ancestor": False}

        def fake_prepare(run_id, base_id, origin="m2"):
            return self.prep_rec(run_id)

        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=fake_prepare)
        # The prepare itself ran; the missing profile blocks the lane.
        self.assertTrue(out["ran"])
        details = [e.get("detail") for e in
                   events.list_events("fakerepo")
                   if e.get("kind") == "m2-blocked"]
        self.assertTrue(any("fastcheck_steps 없음" in (d or "")
                            for d in details))


class NoteMemoryTests(DriverFixture):
    def test_r3_folded_one_event_per_reason_per_tick(self):
        # D4 supersedes the per-run keys: 3 same-reason skips over 3
        # ticks cost exactly 1 timeline event, not 9.
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(run="run-m2%d" % i) for i in range(3)])
        batches.default_base = lambda repo, lane, **k: {"id": ""}
        for _ in range(3):
            m2.tick_repo("fakerepo", _running="promoted",
                         _prepare=lambda *a, **k: self.fail("no act"))
        skips = [t for t in self.titles()
                 if t.startswith("자율 준비 건너뜀")]
        self.assertEqual(len(skips), 1)
        self.assertIn("3건", skips[0])
        self.assertIn("기준 묶음 없음", skips[0])

    def test_r3_folded_distinct_reasons_stay_separate(self):
        # Two reasons give two summary events (one per reason), with
        # the per-run detail kept in the tick return value.
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(run="run-nobase"),
                        self.waiting(run="run-unver", verified=False)])
        batches.default_base = lambda repo, lane, **k: {"id": ""}
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=lambda *a, **k: self.fail("no act"))
        skips = [t for t in self.titles()
                 if t.startswith("자율 준비 건너뜀")]
        self.assertEqual(len(skips), 2)
        self.assertEqual(len(out["skipped"]), 2)

    def test_r3_folded_keys_bounded_no_per_run_keys(self):
        # Folded keys are a fixed per-reason set (no per-run memory
        # growth), so departed runs leave no per-run keys behind.
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(run="run-gone")])
        batches.default_base = lambda repo, lane, **k: {"id": ""}
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=lambda *a, **k: None)
        notes = sched.load("fakerepo").get("m2_notes") or {}
        self.assertIn("skip-nobase", notes)
        self.assertFalse([k for k in notes if k.endswith("run-gone")])
        self.set_queue([])
        before = self.titles()
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=lambda *a, **k: None)
        # An empty group emits nothing new.
        self.assertEqual(self.titles(), before)


class DriverSafetyTests(unittest.TestCase):
    def test_13_driver_path_makes_no_network_or_push_calls(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "m2.py").read_text(encoding="utf-8")
        for token in ("subprocess", "socket", "urllib", "github.com",
                      "push", " gh ", "ssh", "worktree add"):
            self.assertNotIn(token, src,
                             "driver path must not touch network/git: %s"
                             % token)


class DashboardM2Tests(unittest.TestCase):
    def test_14_u1_card_up_and_glance_links(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="m2card"', src)
        # The card sits right after 지금 상태, before the 묶음 상태 card.
        self.assertLess(src.index('id="m2card"'), src.index("rbCard +"))
        self.assertIn('#/repo/${encodeURIComponent(r.name)}">자율', src)

    def test_15_u2_single_opening_note_u3_stack_u4_guard(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(encoding="utf-8")
        # M2.3: all four stages are live, so every row renders a
        # flip button; one defensive template slot remains but never
        # renders while every stage is live.
        self.assertEqual(src.count("다음 단계에서 열림"), 1)
        self.assertIn('stage === "rollup"', src)
        self.assertIn("m2row", src)
        self.assertIn("keep-all", src)
        self.assertIn("지금 켜도 전환 전까지는 실행되지 않습니다", src)
        self.assertIn("의존성 코드를 이 Mac에서 직접 실행하므로", src)


class BatchResumeDriverTests(DriverFixture):
    def done_rec(self):
        return {"op": "prepare", "op_id": "prep-run-m2b-base-1",
                "status": "done", "dep": "left-hook",
                "target": "2.2.2", "batch_id": "base-1",
                "result": {"path": "/w", "branch": "codex/run-m2b",
                           "head": "h", "fingerprint": "f"}}

    def test_done_prepare_with_receipt_resumes_into_batch(self):
        import json
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        self.set_queue([self.waiting(run="run-m2b")])
        rec = self.done_rec()
        batches.latest_integration_op = lambda run_id: rec
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "prep-run-m2b-base-1.fastcheck.json").write_text(
            json.dumps({"ok": True, "steps": []}), encoding="utf-8")
        calls = []
        real_stage = m2._run_batch_stage
        m2._run_batch_stage = lambda *a, **k: calls.append(a) or {"ran": True}
        try:
            out = m2.tick_repo("fakerepo", _running="promoted")
        finally:
            m2._run_batch_stage = real_stage
        self.assertTrue(out["ran"])
        self.assertEqual(len(calls), 1)
        repo, run_id, lane, got, base_id, summary = calls[0]
        self.assertEqual((repo, run_id, lane, base_id),
                         ("fakerepo", "run-m2b", "next", "base-1"))
        self.assertEqual(got["op_id"], "prep-run-m2b-base-1")
        self.assertTrue(summary["ok"])
        # No fresh prepare ran: the recorded op is resumed, never
        # re-prepared.
        self.assertEqual(out["prepared"], [])

    def test_failed_receipt_stays_recorded_never_retried(self):
        import json
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        self.set_queue([self.waiting(run="run-m2b")])
        batches.latest_integration_op = lambda run_id: self.done_rec()
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "prep-run-m2b-base-1.fastcheck.json").write_text(
            json.dumps({"ok": False, "failed": {"rc": 1}}),
            encoding="utf-8")
        calls = []
        real_stage = m2._run_batch_stage
        m2._run_batch_stage = lambda *a, **k: calls.append(a) or {"ran": True}
        try:
            out = m2.tick_repo("fakerepo", _running="promoted")
        finally:
            m2._run_batch_stage = real_stage
        self.assertEqual(calls, [])
        self.assertEqual(out["prepared"], [])
        self.assertIn("이미 기록된 작업 있음",
                      [s["reason"] for s in out["skipped"]])


class FastcheckResumeTests(DriverFixture):
    """S18a: a DONE prepare with no fastcheck receipt resumes into
    the fastcheck stage (any origin), sharing the per-tick cap. A
    red receipt never re-runs (covered by
    test_failed_receipt_stays_recorded_never_retried)."""

    def done_rec(self, run="run-m2b"):
        return {"op": "prepare", "op_id": "prep-%s-base-1" % run,
                "status": "done", "dep": "left-hook",
                "target": "2.2.2", "batch_id": "base-1",
                "result": {"path": "/w", "branch": "codex/%s" % run,
                           "head": "h", "fingerprint": "f"}}

    def stage_fakes(self, fc_box, batch_box, fc_summary=None):
        fc = mock.patch.object(
            m2, "_run_fastcheck_stage",
            lambda repo, run_id, lane, rec, base_id: (
                fc_box.append((repo, run_id, lane, base_id)),
                {"ran": True,
                 "summary": dict(fc_summary or {"ok": True})})[1])
        fc.start()
        self.addCleanup(fc.stop)
        batch = mock.patch.object(
            m2, "_run_batch_stage",
            lambda repo, run_id, lane, rec, base_id, summary,
            extra_primaries=None: (
                batch_box.append((run_id, base_id)),
                {"ran": True, "batch": "b"})[1])
        batch.start()
        self.addCleanup(batch.stop)

    def test_done_without_receipt_runs_fastcheck_then_batch(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.set_queue([self.waiting(run="run-m2b")])
        batches.latest_integration_op = lambda run_id: self.done_rec()
        fc_box, batch_box = [], []
        self.stage_fakes(fc_box, batch_box)
        out = m2.tick_repo("fakerepo", _running="promoted")
        self.assertEqual(
            fc_box, [("fakerepo", "run-m2b", "next", "base-1")])
        self.assertEqual(batch_box, [("run-m2b", "base-1")])
        # Resumed, never re-prepared.
        self.assertEqual(out["prepared"], [])

    def test_fastcheck_off_stays_recorded(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(run="run-m2b")])
        batches.latest_integration_op = lambda run_id: self.done_rec()
        fc_box, batch_box = [], []
        self.stage_fakes(fc_box, batch_box)
        out = m2.tick_repo("fakerepo", _running="promoted")
        self.assertEqual(fc_box, [])
        self.assertEqual(batch_box, [])
        self.assertIn("이미 기록된 작업 있음",
                      [s["reason"] for s in out["skipped"]])

    def test_resume_shares_cap_oldest_first(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        recs = {"run-m2a": self.done_rec("run-m2a"),
                "run-m2b": self.done_rec("run-m2b")}
        batches.latest_integration_op = lambda run_id: recs.get(run_id)
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 0, "tie": 1, "via_ancestor": False}
        self.set_queue([self.waiting(run="run-m2a",
                                     created_at="2026-10-21T00:00:00+00:00"),
                        self.waiting(run="run-m2b",
                                     created_at="2026-10-21T01:00:00+00:00"),
                        self.waiting(run="run-m2c",
                                     created_at="2026-10-21T02:00:00+00:00")])
        fc_box, batch_box = [], []
        self.stage_fakes(fc_box, batch_box)
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run_id, base_id, origin="m2": (
                box.append(run_id),
                {"status": "done",
                 "result": {"branch": "codex/x", "head": "h"}})[1])
        # Default cap 1: only the oldest receipt-less done runs.
        self.assertEqual(
            fc_box, [("fakerepo", "run-m2a", "next", "base-1")])
        self.assertEqual(box, [])
        self.assertIn("자율 준비 대기 2건", self.titles())


class BacklogKindTests(DriverFixture):
    """M2.5b wording: the backlog counts dep-update runs only."""

    def test_issue_runs_excluded_from_review_count(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([
            self.waiting(run="run-dep", verified=False,
                         kind="dep-update"),
            self.waiting(run="run-iss", verified=False,
                         kind="issue-triage", nodep=True)])
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 1, "tie": 1, "via_ancestor": False}
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: self.fail("must not prepare"))
        self.assertFalse(out["ran"])
        self.assertIn("검토 대기 1건 — 리뷰 승인 후 자율 준비",
                      self.titles())
        self.assertFalse(any("검토 대기 2건" in t
                             for t in self.titles()))

    def test_issue_only_stays_silent(self):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(run="run-iss", verified=False,
                                     kind="issue-triage", nodep=True)])
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 1, "tie": 1, "via_ancestor": False}
        m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: self.fail("must not prepare"))
        self.assertFalse(any("검토 대기" in t for t in self.titles()))


class BacklogNoteTests(DriverFixture):
    def test_unverified_backlog_named(self):
        # M3.3a glance backlog: waiting items exist but review has
        # not accepted any of them, so nothing can act. The driver
        # must still say plainly how many wait and where they go.
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self.waiting(run="run-u1", verified=False),
                        self.waiting(run="run-u2", verified=False)])
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 1, "tie": 1, "via_ancestor": False}
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda *a, **k: self.fail("must not prepare"))
        self.assertEqual(out.get("prepared"), [])
        self.assertIn("검토 대기 2건 — 리뷰 승인 후 자율 준비",
                      self.titles())


class CountedBlockTests(DriverFixture):
    """L2: a fastcheck red stamps its counted contribution; a
    passing re-run clears exactly that."""

    def opfile(self, op, stamp=None):
        import json as _json
        rec = {"op": "prepare", "op_id": op, "status": "done"}
        if stamp is not None:
            rec["counted_block"] = stamp
        with open(batches.op_file(op), "w",
                   encoding="utf-8") as fh:
            _json.dump(rec, fh)
        return rec

    def profiled(self):
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["echo", "x"],
                                             "gate": "unit"}],
                                  "toolchain": []}})

    def test_red_stage_stamps_and_counts_once(self):
        self.profiled()
        # FIX-a1: the stage entry enforces the enabled prefix —
        # this red-stamp path runs with prepare+fastcheck on.
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        rec = {"op_id": "op-stamp-9", "dep": "x", "target": "1",
               "result": {"path": str(self.home), "head": "h",
                          "fingerprint": "f"}}
        fail = lambda *a: {"ok": False,
                           "failed": {"argv": ["x"],
                                    "log_tail": "boom"},
                           "steps": []}
        out = m2._run_fastcheck_stage("fakerepo", "run-s9", "next",
                                      rec, "base-1", _runner=fail)
        self.assertFalse(out["ran"])
        import json as _json
        stored = _json.loads(batches.op_file(
            "op-stamp-9").read_text(encoding="utf-8"))
        self.assertEqual(stored["counted_block"]["lane"], "next")
        with sched.locked("fakerepo"):
            blocks = (sched.load("fakerepo").get("m2_consec_blocks")
                      or {})
        self.assertEqual(blocks.get("next"), 1)

    def test_stale_stamp_releases_nothing_after_reset(self):
        # FIX-b2 (Codex F12): failure A stamps generation 0; a
        # later success resets (new generation 1); failure B stamps
        # generation 1; then A passes a manual rerun. B's
        # contribution must survive — the count stays 1.
        self.opfile("op-f12-a")
        self.opfile("op-f12-b")
        m2._record_block("fakerepo", "next", "run-a", "boom-a")
        m2._stamp_fc_block("fakerepo", "next", {"op_id": "op-f12-a"})
        m2._clear_blocks("fakerepo", "next")
        m2._record_block("fakerepo", "next", "run-b", "boom-b")
        m2._stamp_fc_block("fakerepo", "next", {"op_id": "op-f12-b"})
        self.assertTrue(m2.release_counted_block("fakerepo",
                                                 "op-f12-a"))
        with sched.locked("fakerepo"):
            blocks = (sched.load("fakerepo").get("m2_consec_blocks")
                      or {})
        self.assertEqual(blocks.get("next"), 1)
        # Same-generation release still works: B's rerun clears it.
        self.assertTrue(m2.release_counted_block("fakerepo",
                                                 "op-f12-b"))
        with sched.locked("fakerepo"):
            blocks = (sched.load("fakerepo").get("m2_consec_blocks")
                      or {})
        self.assertEqual(blocks.get("next"), 0)

    def test_release_decrements_same_epoch(self):
        self.opfile("op-rel-1", {"lane": "next", "at": "t",
                                 "epoch": 0})
        self.set_state(m2_consec_blocks={"next": 1})
        self.assertTrue(m2.release_counted_block("fakerepo",
                                                 "op-rel-1"))
        with sched.locked("fakerepo"):
            blocks = (sched.load("fakerepo").get("m2_consec_blocks")
                      or {})
        self.assertEqual(blocks.get("next"), 0)
        self.assertIsNone(batches.clear_counted_block("op-rel-1"))

    def test_release_without_stamp_is_false(self):
        self.opfile("op-rel-2")
        self.set_state(m2_consec_blocks={"next": 2})
        self.assertFalse(m2.release_counted_block("fakerepo",
                                                  "op-rel-2"))
        with sched.locked("fakerepo"):
            blocks = (sched.load("fakerepo").get("m2_consec_blocks")
                      or {})
        self.assertEqual(blocks.get("next"), 2)

    def test_release_lifts_cooldown_opened_by_stamp(self):
        self.opfile("op-rel-3", {"lane": "next", "at": "t",
                                 "epoch": 0})
        self.set_state(m2_consec_blocks={},
                       m2_cooldown_until={"next": "2026-10-23T00:00:00+00:00"},
                       m2_block_epoch={"next": 1})
        self.assertTrue(m2.release_counted_block("fakerepo",
                                                 "op-rel-3"))
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
        self.assertNotIn("next", st.get("m2_cooldown_until") or {})
        self.assertIn("대기 해제", " ".join(self.titles()))

    def test_release_consumed_history_keeps_cooldown(self):
        self.opfile("op-rel-4", {"lane": "next", "at": "t",
                                 "epoch": 0})
        self.set_state(m2_consec_blocks={"next": 1},
                       m2_cooldown_until={"next": "2026-10-23T00:00:00+00:00"},
                       m2_block_epoch={"next": 1})
        self.assertTrue(m2.release_counted_block("fakerepo",
                                                 "op-rel-4"))
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
        self.assertIn("next", st.get("m2_cooldown_until") or {})
        self.assertEqual(
            (st.get("m2_consec_blocks") or {}).get("next"), 1)


class StagePrefixTests(DriverFixture):
    # FIX-a1 (Codex F8): a disabled predecessor stage stops later
    # stages. prepare ON + fastcheck OFF + batch ON must NOT batch
    # an op that passed fastcheck earlier — the glance already says
    # batch cannot act, and execution must agree.

    def _done_rec(self, run="run-a1"):
        return {"op": "prepare", "op_id": "prep-run-a1-base-9",
                "run_id": run, "repo": "fakerepo", "lane": "next",
                "dep": "left-hook", "target": "2.2.2",
                "status": "done", "origin": "m2", "batch_id": "base-9",
                "result": {"head": "a" * 40, "branch": "codex/a1",
                           "path": "", "fingerprint": "f" * 64}}

    def _setup_resume(self, run="run-a1"):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "batch", True)
        self.set_queue([self.waiting(run=run)])
        batches.latest_integration_op = (
            lambda rid: self._done_rec(run) if rid == run else None)
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        import json as _json
        (ops / "prep-run-a1-base-9.fastcheck.json").write_text(
            _json.dumps({"ok": True}), encoding="utf-8")
        calls = []
        real = m2._run_batch_stage
        m2._run_batch_stage = (
            lambda *a, **k: (calls.append((a, k)),
                             {"ran": False, "reason": "mock"})[1])
        self.addCleanup(setattr, m2, "_run_batch_stage", real)
        return calls

    def test_batch_refused_when_fastcheck_off(self):
        calls = self._setup_resume()
        out = m2.tick_repo("fakerepo", _running="promoted-sha")
        self.assertEqual(calls, [])
        reasons = " ".join(
            str(s.get("reason") or "") for s in (out.get("skipped")
                                                 or []))
        self.assertIn("동작 안 함", reasons)
        self.assertFalse(any(
            "already batched" in str(s.get("reason") or "")
            for s in (out.get("skipped") or [])))

    def test_all_three_on_still_batches(self):
        calls = self._setup_resume()
        m2.set_policy("fakerepo", "fastcheck", True)
        real_open = dict(m2.STAGE_OPEN)
        m2.STAGE_OPEN["fastcheck"] = True
        self.addCleanup(m2.STAGE_OPEN.update, real_open)
        m2.tick_repo("fakerepo", _running="promoted-sha")
        self.assertEqual(len(calls), 1)


class PrepareTitleTickTests(DriverFixture):
    # M2.8a2: the done title shows from -> to with the major flag,
    # read from the queue item's own installed/declared data.

    def test_done_title_shows_from_to_and_major(self):
        m2.set_policy("fakerepo", "prepare", True)
        item = self.waiting(run="run-t1")
        item.update({"dep": "expo", "target": "58.0.17",
                     "installed": "57.0.11",
                     "declared": "^57.0.11"})
        self.set_queue([item])
        batches.default_base = lambda repo, lane, **k: {"id": "base-1",
                                                   "kin": 2, "tie": 1,
                                                   "via_ancestor": False}

        def fake_prepare(run_id, base_id, origin="m2"):
            return {"op": "prepare",
                    "op_id": f"prep-{run_id}-base-1",
                    "run_id": run_id, "status": "done",
                    "result": {"branch": "codex/t1",
                               "head": "a" * 40}}
        out = m2.tick_repo("fakerepo", _running="promoted-sha",
                           _prepare=fake_prepare)
        self.assertTrue(out["ran"])
        self.assertIn(
            "자율 준비 완료 expo 57.0.11 -> 58.0.17 (메이저)",
            self.titles())


class StaleResumeTests(DriverFixture):
    # FIX-a4: a stale-marked blocked record is refresh-stage work
    # on every tick — an honest skip, never a block and never a
    # cooldown count.

    def test_stale_record_is_skip_never_block(self):
        m2.set_policy("fakerepo", "prepare", True)
        run = "run-stale"
        self.set_queue([self.waiting(run=run)])
        rec = {"op": "prepare", "op_id": "prep-run-stale-base",
               "run_id": run, "repo": "fakerepo", "lane": "next",
               "dep": "left-hook", "target": "2.2.2",
               "status": "blocked", "origin": "m2",
               "batch_id": "base",
               "error": "기준이 이미 bob를 0.43.2로 올림 — 0.44.1 후보를 "
                        "현재 기준에서 다시 만들어야 함",
               "stale": {"dep": "bob", "target": "0.44.1",
                         "base_version": "0.43.2"},
               "result": {"head": "", "branch": "",
                          "conflicted_paths": ["package.json"]}}
        batches.latest_integration_op = (
            lambda rid: rec if rid == run else None)
        out = m2.tick_repo("fakerepo", _running="promoted-sha")
        reasons = " ".join(
            str(s.get("reason") or "") for s in (out.get("skipped")
                                                 or []))
        self.assertIn("후보가 기준보다 오래됨", reasons)
        self.assertNotIn("자율 준비 막힘", " ".join(self.titles()))
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})
        self.assertNotIn("next", st.get("m2_cooldown_until") or {})


class RederiveStrandTests(DriverFixture):
    # M2.13: a done in-prepare rederive tied same-second with its
    # blocked prepare resumes into fastcheck — the run must not
    # strand under "already recorded".
    def test_tied_rederive_resumes_fastcheck(self):
        from openmainty.util import write_json
        for mod, name, real in self._patched:
            if name == "latest_integration_op":
                batches.latest_integration_op = real
        at = "2026-10-23T05:59:32+00:00"
        run = "run-m2r"
        prep_id = "prep-%s-base-1" % run
        base = {"schema": 1, "op": "prepare", "op_id": prep_id,
                "repo": "fakerepo", "run_id": run, "batch_id": "base-1",
                "dep": "left-hook", "target": "2.2.2", "lane": "next",
                "status": "blocked", "error": "pick",
                "created_at": at, "updated_at": at,
                "result": {"path": "/w", "branch": "codex/x",
                           "head": "", "fingerprint": ""}}
        write_json(batches.op_file(prep_id), base)
        write_json(batches.op_file(prep_id + "-rederive"), dict(
            base, op="reresolve", derived_from=prep_id,
            kind="rederive", status="done", error="",
            result={"path": "/w", "branch": "codex/x",
                    "head": "h", "fingerprint": "f"}))
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.set_queue([self.waiting(run=run)])
        fc_box, batch_box = [], []
        fc = mock.patch.object(
            m2, "_run_fastcheck_stage",
            lambda repo, run_id, lane, rec, base_id: (
                fc_box.append((repo, run_id, lane, base_id)),
                {"ran": True,
                 "summary": {"ok": True}})[1])
        fc.start()
        self.addCleanup(fc.stop)
        batch = mock.patch.object(
            m2, "_run_batch_stage",
            lambda repo, run_id, lane, rec, base_id, summary,
            extra_primaries=None: (
                batch_box.append((run_id, base_id)),
                {"ran": True, "batch": "b"})[1])
        batch.start()
        self.addCleanup(batch.stop)
        out = m2.tick_repo("fakerepo", _running="promoted")
        self.assertEqual(
            fc_box, [("fakerepo", run, "next", "base-1")])
        self.assertNotIn("이미 기록된 작업 있음",
                         [s["reason"] for s in out["skipped"]])
        self.assertEqual(out["prepared"], [])


class JointDriverTests(DriverFixture):
    # M2.14b: a done joint covering two blocked prepares resumes
    # fastcheck exactly once per tick — the partner rides along via
    # the joint record instead of running the same op twice.
    def test_done_joint_resumes_fastcheck_once(self):
        from openmainty.util import write_json
        for mod, name, real in self._patched:
            if name == "latest_integration_op":
                batches.latest_integration_op = real
        at = "2026-10-23T07:00:00+00:00"
        x_run, y_run = "run-jx", "run-jy"
        x_op = "prep-%s-base-1" % x_run
        y_op = "prep-%s-base-1" % y_run

        def prep(op_id, run):
            return {"schema": 1, "op": "prepare", "op_id": op_id,
                    "repo": "fakerepo", "run_id": run,
                    "batch_id": "base-1", "dep": "left-hook",
                    "target": "2.2.2", "lane": "next",
                    "status": "blocked", "error": "pick",
                    "created_at": at, "updated_at": at,
                    "result": {"path": "/w", "branch": "codex/x",
                               "head": "", "fingerprint": ""}}
        write_json(batches.op_file(x_op), prep(x_op, x_run))
        write_json(batches.op_file(y_op), prep(y_op, y_run))
        joint_id = x_op + "-joint"
        write_json(batches.op_file(joint_id), dict(
            prep(x_op, x_run), op="reresolve", op_id=joint_id,
            derived_from=x_op, kind="joint", joint_with=[y_op],
            runs=[x_run, y_run], status="done", error="",
            result={"path": "/w", "branch": "codex/j",
                    "head": "h", "fingerprint": "f"}))
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.set_queue([self.waiting(run=x_run),
                        self.waiting(run=y_run)])
        fc_box = []
        fc = mock.patch.object(
            m2, "_run_fastcheck_stage",
            lambda repo, run_id, lane, rec, base_id: (
                fc_box.append((run_id, rec.get("op_id"))),
                {"ran": True,
                 "summary": {"ok": True}})[1])
        fc.start()
        self.addCleanup(fc.stop)
        out = m2.tick_repo("fakerepo", _running="promoted")
        self.assertEqual(len(fc_box), 1)
        self.assertEqual(fc_box[0][1], joint_id)
        self.assertIn("함께 올림 기록이 이번 틱에 이미 실행 중",
                      [s["reason"] for s in out["skipped"]])
        self.assertNotIn("이미 기록된 작업 있음",
                         [s["reason"] for s in out["skipped"]])


class M217PreprepareTests(DriverFixture):
    # M2.17 A: the batch stage would refuse these singletons, so
    # the driver skips the prepare+fastcheck spend with a grouped
    # reason. Never a block, never a cooldown count; the
    # candidate stays queued for a human manual prepare.
    LOCK_CORE7 = "\n".join([
        '"@babel/core@^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
    ])

    def _tree(self, expo_range="^57.0.0", rn_range="^0.86.0"):
        from openmainty.util import write_json
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir(exist_ok=True)
        write_json(wt / "package.json",
                   {"dependencies": {"expo": expo_range,
                                     "react-native": rn_range,
                                     "@babel/core": "^7.29.7"}})
        (wt / "yarn.lock").write_text(self.LOCK_CORE7,
                                      encoding="utf-8")
        batch = {"id": "base-1", "repo": "fakerepo",
                 "target_branch": "next",
                 "integrations": [{"lane": "next",
                                   "worktree": str(wt),
                                   "status": "testing", "gates": []}]}
        write_json(batches.batch_file("base-1"), batch)
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 0, "tie": 1,
            "via_ancestor": False}
        return wt

    def _item(self, run, dep, target, verified=True):
        return {"run_id": run, "status": "waiting-review",
                "lane": "next", "dep": dep, "target": target,
                "verified": verified,
                "created_at": "2026-10-23T00:00:00+00:00"}

    def test_a1_expo58_on_rn86_base_skips_preprepare(self):
        # Live run-23fdef89: expo 57.0.11 -> 58.0.19 prepared and
        # fastchecked 5/5, then refused for the mixed RN tree.
        self._tree()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-expo", "expo", "58.0.19"),
                        self._item("run-rn", "react-native", "0.88.0",
                                   verified=False)])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": box.append(run))
        self.assertEqual(box, [])
        reasons = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn("run-expo", reasons)
        self.assertIn("단독 준비 건너뜀", reasons["run-expo"])
        self.assertNotIn("자율 준비 막힘", " ".join(self.titles()))
        self.assertEqual(
            sched.load("fakerepo").get("m2_consec_blocks"), {})

    def test_a2_runtime8_with_compiler7_skips_preprepare(self):
        self._tree()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-rt", "@babel/runtime",
                                   "8.0.8")])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": box.append(run))
        self.assertEqual(box, [])
        reasons = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(
            reasons.get("run-rt"),
            "컴파일러와 메이저 불일치 "
            "(@babel/core 7 · @babel/runtime 8)")
        self.assertNotIn("자율 준비 막힘", " ".join(self.titles()))

    def test_a3_normal_patch_still_prepares(self):
        self._tree()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-m2a", "left-hook", "2.2.2")])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": (
                box.append(run),
                {"status": "done", "dep": "left-hook",
                 "target": "2.2.2",
                 "result": {"branch": "codex/m2a",
                            "head": "abc123"}})[1])
        self.assertEqual(box, ["run-m2a"])
        self.assertTrue(out["ran"])


class M220BEcosystemTests(DriverFixture):
    # M2.20b: a MAJOR the installed toolchain cannot take waits
    # instead of spending a prepare+fastcheck. The rule is
    # computed from the base lockfile's hard dependencies, never
    # hard-coded; peer `*` alone never blocks; minors are never
    # checked; the note fires once per blocker set, not per tick.
    LOCK_BLOCKED = "\n".join([
        '"@babel/core@npm:^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
        '"metro-babel-transformer@npm:^0.87.1":',
        "  version: 0.87.1",
        '  resolution: "metro-babel-transformer@npm:0.87.1"',
        "  dependencies:",
        '    "@babel/core": "npm:^7.25.2"',
        "  peerDependencies:",
        '    "@babel/core": "*"',
        "",
    ])
    LOCK_PEER_ONLY = "\n".join([
        '"@babel/core@npm:^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
        '"metro-babel-transformer@npm:^0.87.1":',
        "  version: 0.87.1",
        '  resolution: "metro-babel-transformer@npm:0.87.1"',
        "  peerDependencies:",
        '    "@babel/core": "*"',
        "",
    ])
    LOCK_ADMITS = "\n".join([
        '"@babel/core@npm:^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
        '"metro-babel-transformer@npm:^0.87.1":',
        "  version: 0.87.1",
        '  resolution: "metro-babel-transformer@npm:0.87.1"',
        "  dependencies:",
        '    "@babel/core": "npm:>=7.25.2 <9"',
        "",
    ])

    def _tree(self, lock=None):
        from openmainty.util import write_json
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir(exist_ok=True)
        write_json(wt / "package.json",
                   {"dependencies": {"@babel/core": "^7.29.7"}})
        (wt / "yarn.lock").write_text(
            lock if lock is not None else self.LOCK_BLOCKED,
            encoding="utf-8")
        batch = {"id": "base-1", "repo": "fakerepo",
                 "target_branch": "next",
                 "integrations": [{"lane": "next",
                                   "worktree": str(wt),
                                   "status": "testing", "gates": []}]}
        write_json(batches.batch_file("base-1"), batch)
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 0, "tie": 1,
            "via_ancestor": False}
        return wt

    def _item(self, run, dep, target):
        return {"run_id": run, "status": "waiting-review",
                "lane": "next", "dep": dep, "target": target,
                "verified": True,
                "created_at": "2026-10-24T00:00:00+00:00"}

    def _prepared(self, box):
        return lambda run, base, origin="m2": (
            box.append(run),
            {"status": "done", "dep": "x", "target": "y",
             "result": {"branch": "codex/m2a",
                        "head": "abc123"}})[1]

    def test_core8_blocked_with_blocker_text(self):
        self._tree()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-core8", "@babel/core",
                                   "8.0.9")])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self._prepared(box))
        self.assertEqual(box, [])
        reasons = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn("생태계 대기", reasons.get("run-core8", ""))
        self.assertIn("metro-babel-transformer@0.87.1 needs "
                      "@babel/core ^7.25.2",
                      reasons.get("run-core8", ""))
        self.assertNotIn("자율 준비 막힘", " ".join(self.titles()))
        self.assertEqual(
            sched.load("fakerepo").get("m2_consec_blocks"), {})

    def test_peer_star_only_not_blocked(self):
        self._tree(lock=self.LOCK_PEER_ONLY)
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-core8", "@babel/core",
                                   "8.0.9")])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self._prepared(box))
        self.assertEqual(box, ["run-core8"])
        reasons = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertNotIn("run-core8", reasons)

    def test_minor_not_affected(self):
        self._tree()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-core7", "@babel/core",
                                   "7.30.0")])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self._prepared(box))
        self.assertEqual(box, ["run-core7"])
        self.assertTrue(out["ran"])

    def test_admitting_range_unblocks_next_scan(self):
        wt = self._tree()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-core8", "@babel/core",
                                   "8.0.9")])
        box = []
        prep = self._prepared(box)
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=prep)
        self.assertEqual(box, [])
        self.assertIn("run-core8",
                      {s["run"] for s in out["skipped"]})
        (wt / "yarn.lock").write_text(self.LOCK_ADMITS,
                                      encoding="utf-8")
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=prep)
        self.assertEqual(box, ["run-core8"])

    def test_no_repeated_notes_across_ticks(self):
        self._tree()
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._item("run-core8", "@babel/core",
                                   "8.0.9")])
        box = []
        prep = self._prepared(box)
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=prep)
        first = [t for t in self.titles() if "생태계 대기" in t]
        self.assertEqual(len(first), 1)
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=prep)
        again = [t for t in self.titles() if "생태계 대기" in t]
        self.assertEqual(len(again), 1)


class M220CEcoLabelsTests(DriverFixture):
    # M2.20c: the hold names WHO can unblock it. A direct-dep
    # excluder with a newer admitting version in the lock is a
    # "함께 올려야 함" family; without knowable newer data it is
    # "확인 필요", never a guess. A nested-copy build tool keeps
    # "생태계 대기" with a 참고 line. Mixed/transitive excluders
    # (the babel live shape) keep plain "생태계 대기".
    LOCK_ESLINT9 = "\n".join([
        '"@eslint/js@npm:9.39.5":',
        "  version: 9.39.5",
        '  resolution: "@eslint/js@npm:9.39.5"',
        "",
        '"eslint@npm:^9.0.0":',
        "  version: 9.39.5",
        '  resolution: "eslint@npm:9.39.5"',
        "  dependencies:",
        '    "@eslint/js": "npm:9.39.5"',
        "",
    ])
    LOCK_ESLINT9_10 = "\n".join([
        '"@eslint/js@npm:9.39.5":',
        "  version: 9.39.5",
        '  resolution: "@eslint/js@npm:9.39.5"',
        "",
        '"eslint@npm:^9.0.0":',
        "  version: 9.39.5",
        '  resolution: "eslint@npm:9.39.5"',
        "  dependencies:",
        '    "@eslint/js": "npm:9.39.5"',
        "",
        '"eslint@npm:^10.0.0":',
        "  version: 10.0.0",
        '  resolution: "eslint@npm:10.0.0"',
        "  dependencies:",
        '    "@eslint/js": "npm:^10.0.0"',
        "",
    ])
    LOCK_BOB_TS = "\n".join([
        '"typescript@npm:^6.0.3":',
        "  version: 6.0.3",
        '  resolution: "typescript@npm:6.0.3"',
        "",
        '"react-native-builder-bob@npm:^0.43.0":',
        "  version: 0.43.2",
        '  resolution: "react-native-builder-bob@npm:0.43.2"',
        "  dependencies:",
        '    "typescript": "npm:^6.0.3"',
        "",
    ])
    LOCK_BABEL_MIX = "\n".join([
        '"@babel/core@npm:^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
        '"react-native-builder-bob@npm:^0.43.0":',
        "  version: 0.43.2",
        '  resolution: "react-native-builder-bob@npm:0.43.2"',
        "  dependencies:",
        '    "@babel/core": "npm:^7.29.0"',
        "",
        '"@react-native/babel-preset@npm:0.86.2":',
        "  version: 0.86.2",
        '  resolution: "@react-native/babel-preset@npm:0.86.2"',
        "  dependencies:",
        '    "@babel/core": "npm:^7.25.2"',
        "",
        '"metro-babel-transformer@npm:^0.84.0":',
        "  version: 0.84.4",
        '  resolution: "metro-babel-transformer@npm:0.84.4"',
        "  dependencies:",
        '    "@babel/core": "npm:^7.25.2"',
        "",
    ])

    def _tree(self, lock, direct_deps):
        from openmainty.util import write_json
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir(exist_ok=True)
        write_json(wt / "package.json",
                   {"devDependencies": dict(direct_deps)})
        (wt / "yarn.lock").write_text(lock, encoding="utf-8")
        batch = {"id": "base-1", "repo": "fakerepo",
                 "target_branch": "next",
                 "integrations": [{"lane": "next",
                                   "worktree": str(wt),
                                   "status": "testing", "gates": []}]}
        write_json(batches.batch_file("base-1"), batch)
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 0, "tie": 1,
            "via_ancestor": False}
        return wt

    def _item(self, run, dep, target):
        return {"run_id": run, "status": "waiting-review",
                "lane": "next", "dep": dep, "target": target,
                "verified": True,
                "created_at": "2026-10-24T00:00:00+00:00"}

    def _tick(self, dep, target):
        self.set_queue([self._item("run-x", dep, target)])
        box = []
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=lambda run, base, origin="m2": (
                box.append(run),
                {"status": "done", "dep": "x", "target": "y",
                 "result": {"branch": "codex/m2a",
                            "head": "abc123"}})[1])
        reasons = {s["run"]: s["reason"] for s in out["skipped"]}
        return box, reasons.get("run-x", "")

    def test_together_when_newer_admits(self):
        self._tree(self.LOCK_ESLINT9_10, {"eslint": "^9.0.0"})
        m2.set_policy("fakerepo", "prepare", True)
        box, reason = self._tick("@eslint/js", "10.0.1")
        self.assertEqual(box, [])
        self.assertIn("함께 올려야 함", reason)
        self.assertIn("eslint 10.0.0 + @eslint/js 10.0.1",
                      reason)
        self.assertIn("주인 결정", reason)

    def test_confirm_needed_without_newer_data(self):
        self._tree(self.LOCK_ESLINT9, {"eslint": "^9.0.0"})
        m2.set_policy("fakerepo", "prepare", True)
        box, reason = self._tick("@eslint/js", "10.0.1")
        self.assertEqual(box, [])
        self.assertIn("함께 올려야 할 수 있음", reason)
        self.assertIn("eslint", reason)
        self.assertIn("확인 필요", reason)

    def test_transitive_keeps_waiting(self):
        self._tree(M220BEcosystemTests.LOCK_BLOCKED,
                   {"@babel/core": "^7.29.7"})
        m2.set_policy("fakerepo", "prepare", True)
        box, reason = self._tick("@babel/core", "8.0.9")
        self.assertEqual(box, [])
        self.assertIn("생태계 대기", reason)
        self.assertNotIn("함께", reason)
        self.assertNotIn("참고", reason)

    def test_nested_tool_gets_reference_line(self):
        self._tree(self.LOCK_BOB_TS, {"typescript": "^6.0.3"})
        m2.set_policy("fakerepo", "prepare", True)
        box, reason = self._tick("typescript", "7.0.2")
        self.assertEqual(box, [])
        self.assertIn("생태계 대기", reason)
        self.assertIn("참고", reason)
        self.assertIn("react-native-builder-bob keeps its own "
                      "typescript; peer ranges not checked",
                      reason)

    def test_babel_live_shape_unchanged(self):
        # Direct excluders (bob, preset) plus a transitive one
        # (metro): mixed excluders keep plain 생태계 대기.
        self._tree(self.LOCK_BABEL_MIX,
                   {"@babel/core": "^7.29.7",
                    "react-native-builder-bob": "^0.43.0",
                    "@react-native/babel-preset": "0.86.2"})
        m2.set_policy("fakerepo", "prepare", True)
        box, reason = self._tick("@babel/core", "8.0.9")
        self.assertEqual(box, [])
        self.assertIn("생태계 대기", reason)
        self.assertNotIn("함께", reason)
        self.assertNotIn("참고", reason)

    def test_direct_before_transitive(self):
        # M2.20d: the owner recognises its own deps first — the
        # direct preset lists before transitive @expo/metro-config.
        lock = "\n".join([
            '"@babel/core@npm:^7.29.7":',
            "  version: 7.29.7",
            '  resolution: "@babel/core@npm:7.29.7"',
            "",
            '"@react-native/babel-preset@npm:0.86.2":',
            "  version: 0.86.2",
            '  resolution: "@react-native/babel-preset@npm:0.86.2"',
            "  dependencies:",
            '    "@babel/core": "npm:^7.25.2"',
            "",
            '"@expo/metro-config@npm:57.0.7":',
            "  version: 57.0.7",
            '  resolution: "@expo/metro-config@npm:57.0.7"',
            "  dependencies:",
            '    "@babel/core": "npm:^7.20.0"',
            "",
        ])
        self._tree(lock, {"@babel/core": "^7.29.7",
                          "@react-native/babel-preset": "0.86.2"})
        m2.set_policy("fakerepo", "prepare", True)
        box, reason = self._tick("@babel/core", "8.0.9")
        self.assertEqual(box, [])
        self.assertIn("생태계 대기", reason)
        self.assertLess(reason.index("@react-native/babel-preset"),
                        reason.index("@expo/metro-config"))

    def test_dependents_before_alphabetical(self):
        # M2.20d: of two transitive excluders, the one more
        # packages depend on lists first — zebra (2 dependents)
        # before apple (0), against alphabetical order.
        lock = "\n".join([
            '"corelib@npm:^7.0.0":',
            "  version: 7.0.0",
            '  resolution: "corelib@npm:7.0.0"',
            "",
            '"zebra@npm:^1.0.0":',
            "  version: 1.0.0",
            '  resolution: "zebra@npm:1.0.0"',
            "  dependencies:",
            '    "corelib": "npm:^7.0.0"',
            "",
            '"apple@npm:^1.0.0":',
            "  version: 1.0.0",
            '  resolution: "apple@npm:1.0.0"',
            "  dependencies:",
            '    "corelib": "npm:^7.0.0"',
            "",
            '"top1@npm:^1.0.0":',
            "  version: 1.0.0",
            '  resolution: "top1@npm:1.0.0"',
            "  dependencies:",
            '    "zebra": "npm:^1.0.0"',
            "",
            '"top2@npm:^1.0.0":',
            "  version: 1.0.0",
            '  resolution: "top2@npm:1.0.0"',
            "  dependencies:",
            '    "zebra": "npm:^1.0.0"',
            "",
        ])
        self._tree(lock, {"corelib": "^7.0.0"})
        m2.set_policy("fakerepo", "prepare", True)
        box, reason = self._tick("corelib", "8.0.0")
        self.assertEqual(box, [])
        self.assertIn("생태계 대기", reason)
        self.assertLess(reason.index("zebra@1.0.0"),
                        reason.index("apple@1.0.0"))


class M217JointRefreshTests(DriverFixture):
    # M2.17 B: when a newer target of a done joint's member
    # appears, the driver derives ONE refreshed joint through the
    # M2.14 path instead of a doomed standalone — at most once
    # per tick. The derive itself is mocked at the stage
    # boundary (the batches tests cover the real flow); these
    # tests cover the driver wiring.
    X_RUN, Y_OLD, Y_NEW = "run-jx", "run-jy17", "run-jy19"
    X_OP = "prep-run-jx-base-1"

    def _old_joint(self):
        from openmainty.util import write_json
        at = "2026-10-23T00:00:00+00:00"
        write_json(batches.op_file(self.X_OP),
                   {"op": "prepare", "op_id": self.X_OP,
                    "repo": "fakerepo", "run_id": self.X_RUN,
                    "batch_id": "base-1", "dep": "react-native",
                    "target": "0.88.0", "lane": "next",
                    "status": "done", "created_at": at,
                    "updated_at": at,
                    "candidate": {"head": "xhead",
                                  "branch": "codex/jx"}})
        joint_op = self.X_OP + "-joint"
        write_json(batches.op_file(joint_op),
                   {"op": "reresolve", "op_id": joint_op,
                    "derived_from": self.X_OP, "kind": "joint",
                    "joint_with": [self.X_OP],
                    "runs": [self.X_RUN, self.Y_OLD],
                    "joint_candidates": [
                        {"run": self.X_RUN, "dep": "react-native",
                         "target": "0.88.0", "head": "xhead"},
                        {"run": self.Y_OLD, "dep": "expo",
                         "target": "58.0.17", "head": "yhead"}],
                    "repo": "fakerepo", "run_id": self.X_RUN,
                    "batch_id": "base-1", "dep": "react-native",
                    "target": "0.88.0", "lane": "next",
                    "status": "done", "error": "",
                    "created_at": at, "updated_at": at,
                    "result": {"path": "/w", "branch": "codex/j",
                               "head": "h", "fingerprint": "f"}})
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 0, "tie": 1,
            "via_ancestor": False}
        return joint_op

    def _y_item(self):
        return {"run_id": self.Y_NEW, "status": "waiting-review",
                "lane": "next", "dep": "expo", "target": "58.0.19",
                "verified": True,
                "created_at": "2026-10-24T00:00:00+00:00"}

    def test_decision_names_refreshed_pair(self):
        old_joint = self._old_joint()
        plan = m2.joint_refresh_for(
            "fakerepo", "next", self._y_item(), "base-1",
            [self._y_item()])
        self.assertIsNotNone(plan)
        self.assertEqual(
            (plan["x_run"], plan["y_run"], plan["x_dep"],
             plan["y_dep"], plan["y_target"]),
            (self.X_RUN, self.Y_NEW, "react-native", "expo",
             "58.0.19"))
        self.assertEqual(plan["x_op_id"], self.X_OP)
        self.assertEqual(plan["old_joint_op"], old_joint)

    def _tick_joint(self, derive_mock, box):
        m2.set_policy("fakerepo", "prepare", True)
        self.set_queue([self._y_item()])
        with mock.patch.object(batches, "derive_joint_refresh",
                               derive_mock):
            return m2.tick_repo(
                "fakerepo", _running="promoted",
                _prepare=lambda run, base, origin="m2": (
                    box.append(run)))

    def test_b1_derives_one_refreshed_joint_no_standalone(self):
        self._old_joint()
        calls = []

        def fake_derive(repo, lane, base_id, x_op_id, y_run_id,
                        by="m2"):
            calls.append((repo, lane, base_id, x_op_id,
                          y_run_id, by))
            return {"status": "done", "op_id": "new-joint",
                    "result": {"head": "jhead"}}

        box = []
        out = self._tick_joint(fake_derive, box)
        self.assertEqual(
            calls,
            [("fakerepo", "next", "base-1", self.X_OP, self.Y_NEW,
              "m2")])
        self.assertEqual(box, [])
        self.assertEqual(
            [p["run"] for p in out["prepared"]], [self.Y_NEW])

    def test_b2_second_tick_does_not_derive_again(self):
        self._old_joint()
        from openmainty.util import write_json
        at = "2026-10-24T01:00:00+00:00"
        write_json(batches.op_file("prep-%s-base-1-joint-new"
                                   % self.Y_NEW),
                   {"op": "reresolve",
                    "op_id": "prep-new-joint",
                    "derived_from": self.X_OP, "kind": "joint",
                    "joint_with": [self.X_OP],
                    "runs": [self.X_RUN, self.Y_NEW],
                    "repo": "fakerepo", "run_id": self.Y_NEW,
                    "batch_id": "base-1", "dep": "expo",
                    "target": "58.0.19", "lane": "next",
                    "status": "done", "error": "",
                    "created_at": at, "updated_at": at,
                    "result": {"path": "/w", "branch": "codex/j2",
                               "head": "h2", "fingerprint": "f2"}})
        # The decision itself already covers the pair.
        self.assertIsNone(m2.joint_refresh_for(
            "fakerepo", "next", self._y_item(), "base-1",
            [self._y_item()]))
        # And the tick resumes the new joint op instead of
        # deriving (fastcheck stage, mocked at the boundary).
        for mod, name, real in self._patched:
            if name == "latest_integration_op":
                batches.latest_integration_op = real
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.set_queue([self._y_item()])
        fc_box = []
        calls = []
        with mock.patch.object(
                m2, "_run_fastcheck_stage",
                lambda repo, run_id, lane, rec, base_id: (
                    fc_box.append(rec.get("op_id")),
                    {"ran": True, "summary": {"ok": True}})[1]), \
            mock.patch.object(
                batches, "derive_joint_refresh",
                lambda *a, **k: calls.append((a, k)) or {
                    "status": "done"}):
            m2.tick_repo("fakerepo", _running="promoted",
                         _prepare=lambda run, base, origin="m2": None)
        self.assertEqual(calls, [])
        self.assertEqual(fc_box, ["prep-new-joint"])

    def test_b3_refused_derive_blocks_and_skips_standalone(self):
        self._old_joint()

        def fake_raise(repo, lane, base_id, x_op_id, y_run_id,
                       by="m2"):
            raise IntegrationError(
                "base integration source moved or dirty; "
                "joint refresh refused")

        box = []
        out = self._tick_joint(fake_raise, box)
        self.assertEqual(box, [])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-blocked", kinds)
        reasons = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn(self.Y_NEW, reasons)

    def test_b3b_blocked_record_blocks_and_skips_standalone(self):
        self._old_joint()

        def fake_blocked(repo, lane, base_id, x_op_id, y_run_id,
                         by="m2"):
            return {"status": "blocked", "op_id": "ref-joint",
                    "error": "함께 올려도 RN 혼재 남음: x"}

        box = []
        out = self._tick_joint(fake_blocked, box)
        self.assertEqual(box, [])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-blocked", kinds)
        reasons = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn(self.Y_NEW, reasons)

    def _joint_batch(self, bid, op_id):
        from openmainty.util import write_json
        write_json(batches.batch_file(bid),
                   {"id": bid, "repo": "fakerepo",
                    "origin_m2": op_id, "primaries": [],
                    "updated_at": "2026-10-24T00:00:00+00:00"})

    def _joint_op(self, op_id, expo_target, updated_at,
                  base="base-1", status="done"):
        from openmainty.util import write_json
        at = "2026-10-23T00:00:00+00:00"
        write_json(batches.op_file(op_id),
                   {"op": "reresolve", "op_id": op_id,
                    "derived_from": self.X_OP, "kind": "joint",
                    "joint_with": [self.X_OP],
                    "runs": [self.X_RUN, self.Y_NEW],
                    "joint_candidates": [
                        {"run": self.X_RUN, "dep": "react-native",
                         "target": "0.88.0", "head": "xhead"},
                        {"run": self.Y_NEW, "dep": "expo",
                         "target": expo_target, "head": "yhead"}],
                    "repo": "fakerepo", "run_id": self.Y_NEW,
                    "batch_id": base, "dep": "expo",
                    "target": expo_target, "lane": "next",
                    "base": {"batch": base},
                    "status": status, "error": "",
                    "created_at": at, "updated_at": updated_at,
                    "result": {"path": "/w", "branch": "codex/j",
                               "head": "h", "fingerprint": "f"}})

    def test_map_old_joint_superseded_by_refresh(self):
        # Op ids keep the prep-*-joint shape the map scans.
        self._joint_op("prep-map-old-joint", "58.0.17",
                       "2026-10-24T00:00:00+00:00")
        self._joint_op("prep-map-new-joint", "58.0.19",
                       "2026-10-24T01:00:00+00:00")
        self._joint_batch("b-old", "prep-map-old-joint")
        self._joint_batch("b-new", "prep-map-new-joint")
        out = batches._joint_supersede_map()
        self.assertEqual(out.get("b-old"), "prep-map-new-joint")
        self.assertNotIn("b-new", out)

    def test_map_no_refresh_no_note(self):
        # Same targets: no refresh, no note.
        self._joint_op("prep-map-old-joint", "58.0.17",
                       "2026-10-24T00:00:00+00:00")
        self._joint_op("prep-map-same-joint", "58.0.17",
                       "2026-10-24T01:00:00+00:00")
        self._joint_batch("b-old", "prep-map-old-joint")
        self._joint_batch("b-same", "prep-map-same-joint")
        out = batches._joint_supersede_map()
        self.assertNotIn("b-old", out)
        self.assertNotIn("b-same", out)

    def test_map_different_base_no_note(self):
        self._joint_op("prep-map-old-joint", "58.0.17",
                       "2026-10-24T00:00:00+00:00")
        self._joint_op("prep-map-other-joint", "58.0.19",
                       "2026-10-24T01:00:00+00:00",
                       base="base-2")
        self._joint_batch("b-old", "prep-map-old-joint")
        self._joint_batch("b-other", "prep-map-other-joint")
        out = batches._joint_supersede_map()
        self.assertNotIn("b-old", out)
        self.assertNotIn("b-other", out)

    def test_pipeline_rec_joint_covers_member_runs(self):
        self._joint_op("j-new", "58.0.19",
                       "2026-10-24T01:00:00+00:00")
        from openmainty.util import read_json
        rec = read_json(batches.op_file("j-new"), default=None)
        # The fresh-run-primary record IS the pipeline record of
        # its covered runs — the next tick resumes it.
        self.assertIs(m2._pipeline_prepare_rec(rec), rec)

    def test_pipeline_rec_blocked_joint_is_not_pipeline(self):
        self._joint_op("j-new", "58.0.19",
                       "2026-10-24T01:00:00+00:00",
                       status="blocked")
        from openmainty.util import read_json
        rec = read_json(batches.op_file("j-new"), default=None)
        self.assertIsNone(m2._pipeline_prepare_rec(rec))

    def test_driver_joint_path_spawns_no_processes(self):
        # m2.py itself never touches git/network: the joint path
        # lives in batches, behind the mocked boundary here.
        self._old_joint()

        def fake_derive(repo, lane, base_id, x_op_id, y_run_id,
                        by="m2"):
            return {"status": "done", "op_id": "new-joint",
                    "result": {"head": "jhead"}}

        box = []
        with mock.patch("subprocess.run",
                        side_effect=AssertionError("no spawn")), \
            mock.patch.object(batches, "derive_joint_refresh",
                              fake_derive):
            m2.set_policy("fakerepo", "prepare", True)
            self.set_queue([self._y_item()])
            out = m2.tick_repo(
                "fakerepo", _running="promoted",
                _prepare=lambda run, base, origin="m2": (
                    box.append(run)))
        self.assertEqual([p["run"] for p in out["prepared"]],
                         [self.Y_NEW])


class M217bRefusedDivertTests(DriverFixture):
    # M2.17b A: an already-derived, batch-refused member diverts
    # to a refreshed joint instead of resuming into another
    # doomed batch. Live: expo 58.0.19 rederive (done, fastcheck
    # 5/5) refused for "RN 그룹 혼재 ... — RN 0.88 그룹과 함께".
    X_RUN, Y_RUN = "run-jx", "run-jy19"
    X_OP = "prep-run-jx-base-1"
    Y_OP = "prep-run-jy19-base-1"
    Y_RE = "prep-run-jy19-base-1-rederive"

    LOCK_BASE = "\n".join([
        '"@react-native/codegen@npm:^0.86.0":',
        "  version: 0.86.2",
        '  resolution: "@react-native/codegen@npm:0.86.2"',
        "",
    ])
    LOCK_MIXED = "\n".join([
        '"@react-native/codegen@npm:^0.86.0":',
        "  version: 0.86.2",
        '  resolution: "@react-native/codegen@npm:0.86.2"',
        "",
        '"@react-native/codegen@npm:^0.88.0":',
        "  version: 0.88.0",
        '  resolution: "@react-native/codegen@npm:0.88.0"',
        "",
    ])

    def _trees(self, derived_lock=None):
        from openmainty.util import write_json
        base_wt = Path(self.tmp.name) / "basewt"
        new_wt = Path(self.tmp.name) / "newwt"
        base_wt.mkdir(exist_ok=True)
        new_wt.mkdir(exist_ok=True)
        (base_wt / "yarn.lock").write_text(self.LOCK_BASE,
                                           encoding="utf-8")
        (new_wt / "yarn.lock").write_text(
            derived_lock if derived_lock is not None
            else self.LOCK_MIXED, encoding="utf-8")
        write_json(batches.batch_file("base-1"),
                   {"id": "base-1", "repo": "fakerepo",
                    "target_branch": "next",
                    "integrations": [{"lane": "next",
                                      "worktree": str(base_wt),
                                      "status": "testing",
                                      "gates": []}]})
        batches.default_base = lambda repo, lane, **k: {
            "id": "base-1", "kin": 0, "tie": 1,
            "via_ancestor": False}
        return str(new_wt)

    def _solo_ops(self, new_wt):
        from openmainty.util import write_json
        at = "2026-10-23T00:00:00+00:00"
        write_json(batches.op_file(self.X_OP),
                   {"op": "prepare", "op_id": self.X_OP,
                    "repo": "fakerepo", "run_id": self.X_RUN,
                    "batch_id": "base-1", "dep": "react-native",
                    "target": "0.88.0", "lane": "next",
                    "status": "done", "created_at": at,
                    "updated_at": at,
                    "candidate": {"head": "xhead",
                                  "branch": "codex/jx"}})
        write_json(batches.op_file(self.Y_OP),
                   {"op": "prepare", "op_id": self.Y_OP,
                    "repo": "fakerepo", "run_id": self.Y_RUN,
                    "batch_id": "base-1", "dep": "expo",
                    "target": "58.0.19", "lane": "next",
                    "status": "blocked", "error": "pick",
                    "created_at": at, "updated_at": at,
                    "candidate": {"head": "yhead",
                                  "branch": "codex/jy"}})
        write_json(batches.op_file(self.Y_RE),
                   {"op": "reresolve", "op_id": self.Y_RE,
                    "derived_from": self.Y_OP, "kind": "rederive",
                    "repo": "fakerepo", "run_id": self.Y_RUN,
                    "batch_id": "base-1", "dep": "expo",
                    "target": "58.0.19", "lane": "next",
                    "status": "done", "error": "",
                    "created_at": at, "updated_at": at,
                    "result": {"path": new_wt, "branch": "codex/jy2",
                               "head": "yh2", "fingerprint": "yf2"}})

    def _old_joint(self):
        from openmainty.util import write_json
        at = "2026-10-23T00:00:00+00:00"
        joint_op = self.X_OP + "-joint"
        write_json(batches.op_file(joint_op),
                   {"op": "reresolve", "op_id": joint_op,
                    "derived_from": self.X_OP, "kind": "joint",
                    "joint_with": [self.X_OP],
                    "runs": [self.X_RUN, "run-jy17"],
                    "joint_candidates": [
                        {"run": self.X_RUN, "dep": "react-native",
                         "target": "0.88.0", "head": "xhead"},
                        {"run": "run-jy17", "dep": "expo",
                         "target": "58.0.17", "head": "yhead"}],
                    "repo": "fakerepo", "run_id": self.X_RUN,
                    "batch_id": "base-1", "lane": "next",
                    "status": "done", "error": "",
                    "created_at": at, "updated_at": at,
                    "result": {"path": "/w", "branch": "codex/j",
                               "head": "h", "fingerprint": "f"}})
        return joint_op

    def _y_item(self):
        return {"run_id": self.Y_RUN, "status": "waiting-review",
                "lane": "next", "dep": "expo", "target": "58.0.19",
                "verified": True,
                "created_at": "2026-10-24T00:00:00+00:00"}

    def _restore_ops(self):
        for mod, name, real in self._patched:
            if name == "latest_integration_op":
                batches.latest_integration_op = real

    def test_decision_names_refused_pair(self):
        new_wt = self._trees()
        self._solo_ops(new_wt)
        old_joint = self._old_joint()
        from openmainty.util import read_json
        rec = read_json(batches.op_file(self.Y_RE), default=None)
        plan = m2.joint_refresh_for_refused(
            "fakerepo", "next", rec, "base-1")
        self.assertIsNotNone(plan)
        self.assertEqual(
            (plan["x_run"], plan["y_run"], plan["y_dep"],
             plan["y_target"]),
            (self.X_RUN, self.Y_RUN, "expo", "58.0.19"))
        self.assertEqual(plan["x_op_id"], self.X_OP)
        self.assertEqual(plan["old_joint_op"], old_joint)

    def test_decision_unmixed_tree_returns_none(self):
        new_wt = self._trees(derived_lock=self.LOCK_BASE)
        self._solo_ops(new_wt)
        self._old_joint()
        from openmainty.util import read_json
        rec = read_json(batches.op_file(self.Y_RE), default=None)
        self.assertIsNone(m2.joint_refresh_for_refused(
            "fakerepo", "next", rec, "base-1"))

    def _tick_refused(self, derive_mock, box, fc_box):
        self._restore_ops()
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        self.set_queue([self._y_item()])
        with mock.patch.object(batches, "derive_joint_refresh",
                               derive_mock), \
            mock.patch.object(
                m2, "_run_fastcheck_stage",
                lambda repo, run_id, lane, rec, base_id: (
                    fc_box.append(rec.get("op_id")),
                    {"ran": True, "summary": {"ok": True}})[1]):
            return m2.tick_repo(
                "fakerepo", _running="promoted",
                _prepare=lambda run, base, origin="m2": (
                    box.append(run)))

    def test_a_derives_one_refreshed_joint_no_resume(self):
        new_wt = self._trees()
        self._solo_ops(new_wt)
        self._old_joint()
        calls = []

        def fake_derive(repo, lane, base_id, x_op_id, y_run_id,
                        by="m2"):
            calls.append((repo, lane, base_id, x_op_id,
                          y_run_id, by))
            return {"status": "done", "op_id": "new-joint",
                    "result": {"head": "jhead"}}

        box, fc_box = [], []
        out = self._tick_refused(fake_derive, box, fc_box)
        self.assertEqual(
            calls,
            [("fakerepo", "next", "base-1", self.X_OP, self.Y_RUN,
              "m2")])
        # The doomed batch is never resumed: no prepare, no
        # fastcheck on the refused rederive.
        self.assertEqual(box, [])
        self.assertEqual(fc_box, [])
        self.assertEqual(
            [p["run"] for p in out["prepared"]], [self.Y_RUN])

    def test_a2_second_tick_does_not_derive_again(self):
        new_wt = self._trees()
        self._solo_ops(new_wt)
        self._old_joint()
        from openmainty.util import write_json
        at = "2026-10-24T01:00:00+00:00"
        write_json(batches.op_file("prep-new-joint"),
                   {"op": "reresolve", "op_id": "prep-new-joint",
                    "derived_from": self.X_OP, "kind": "joint",
                    "joint_with": [self.X_OP],
                    "runs": [self.X_RUN, self.Y_RUN],
                    "repo": "fakerepo", "run_id": self.Y_RUN,
                    "batch_id": "base-1", "dep": "expo",
                    "target": "58.0.19", "lane": "next",
                    "status": "done", "error": "",
                    "created_at": at, "updated_at": at,
                    "result": {"path": "/w2", "branch": "codex/j2",
                               "head": "h2", "fingerprint": "f2"}})
        box, fc_box, calls = [], [], []
        out = self._tick_refused(
            lambda *a, **k: calls.append((a, k)) or {
                "status": "done"},
            box, fc_box)
        self.assertEqual(calls, [])
        # The covered pair resumes the new joint op instead.
        self.assertEqual(fc_box, ["prep-new-joint"])
        self.assertEqual(box, [])

    def test_a_no_covering_joint_resumes_as_today(self):
        new_wt = self._trees()
        self._solo_ops(new_wt)
        box, fc_box, calls = [], [], []
        self._tick_refused(
            lambda *a, **k: calls.append((a, k)) or {
                "status": "done"},
            box, fc_box)
        self.assertEqual(calls, [])
        # No joint covers the partner: the refused rederive
        # resumes into fastcheck exactly as before M2.17b.
        self.assertEqual(fc_box, [self.Y_RE])
        self.assertEqual(box, [])

    def test_a_different_base_means_no_derive(self):
        new_wt = self._trees()
        self._solo_ops(new_wt)
        from openmainty.util import write_json
        at = "2026-10-23T00:00:00+00:00"
        write_json(batches.op_file(self.X_OP + "-joint"),
                   {"op": "reresolve",
                    "op_id": self.X_OP + "-joint",
                    "derived_from": self.X_OP, "kind": "joint",
                    "joint_with": [self.X_OP],
                    "runs": [self.X_RUN, "run-jy17"],
                    "joint_candidates": [
                        {"run": self.X_RUN, "dep": "react-native",
                         "target": "0.88.0", "head": "xhead"},
                        {"run": "run-jy17", "dep": "expo",
                         "target": "58.0.17", "head": "yhead"}],
                    "repo": "fakerepo", "run_id": self.X_RUN,
                    "batch_id": "base-2", "lane": "next",
                    "status": "done", "error": "",
                    "created_at": at, "updated_at": at,
                    "result": {"path": "/w", "branch": "codex/j",
                               "head": "h", "fingerprint": "f"}})
        from openmainty.util import read_json
        rec = read_json(batches.op_file(self.Y_RE), default=None)
        self.assertIsNone(m2.joint_refresh_for_refused(
            "fakerepo", "next", rec, "base-1"))


if __name__ == "__main__":
    unittest.main()
