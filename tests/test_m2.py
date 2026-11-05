"""M2.1a tests: policy toggles, guard, op lock, events, policy API.

Fixture repos live under a temp OPENMAINTY_HOME; the env override is
restored after each test so the suite stays isolated.
"""
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import events, m2, repos, sched, store
try:
    from tests._home import use_temp_home
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home


class M2PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})

    def test_1_defaults_off(self):
        # S15: the refresh toggle defaults OFF like every stage.
        self.assertEqual(m2.get_policy("fakerepo"),
                         {"prepare": False, "fastcheck": False,
                          "batch": False, "rollup": False,
                          "refresh": False})
        self.assertEqual(m2.policy_line(m2.get_policy("fakerepo")), "꺼짐")

    def test_1b_refresh_line_chains_off_prepare(self):
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "refresh", True)
        self.assertEqual(
            m2.policy_line(m2.get_policy("fakerepo")), "준비·재실행만")
        m2.set_policy("fakerepo", "prepare", False)
        self.assertIn("동작 안 함",
                      m2.policy_line(m2.get_policy("fakerepo")))

    def test_e3_flip_on_clears_cooldown_and_counts(self):
        # E5: the fixture uses the REAL cooldown key the tick reads
        # (m2._COOLDOWN_KEY) — the old wrong key made this pass
        # while production stayed cooling.
        from openmainty import events, sched
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st[m2._COOLDOWN_KEY] = {"next": "2999-01-01T00:00:00+00:00"}
            st["m2_consec_blocks"] = {"next": 3}
            sched.save(st)
        m2.set_policy("fakerepo", "prepare", True)
        st = sched.load("fakerepo")
        self.assertEqual(st.get(m2._COOLDOWN_KEY), {})
        self.assertEqual(st.get("m2_consec_blocks"), {})
        evs = events.list_events("fakerepo")
        titles = [e.get("title") for e in evs]
        self.assertIn("대기 해제: 소유자가 다시 켬", titles)
        details = [e.get("detail") or "" for e in evs
                   if e.get("title") == "대기 해제: 소유자가 다시 켬"]
        self.assertTrue(any("next" in d for d in details))
        # S16b wording: release, with the original until-time.
        self.assertTrue(any("대기 해제" in d and "원래" in d
                            for d in details))
        # Flipping on again (or with nothing to clear) stays quiet.
        m2.set_policy("fakerepo", "prepare", True)
        titles2 = [e.get("title") for e in events.list_events("fakerepo")]
        self.assertEqual(len(titles2), len(titles) + 1)  # flip line only

    def test_e5b_flip_with_nothing_cleared_claims_nothing(self):
        from openmainty import events
        m2.set_policy("fakerepo", "prepare", True)
        texts = [(e.get("title") or "") + "\n" + (e.get("detail") or "")
                 for e in events.list_events("fakerepo")]
        self.assertFalse(any("대기 해제" in t for t in texts))

    def test_2_flip_records_event(self):
        out = m2.set_policy("fakerepo", "prepare", True)
        self.assertTrue(out["prepare"])
        self.assertFalse(out["fastcheck"])
        self.assertEqual(m2.policy_line(out), "준비만")
        out = m2.set_policy("fakerepo", "fastcheck", True)
        self.assertEqual(m2.policy_line(out), "준비·검증만")
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-policy", kinds)
        with self.assertRaises(ValueError):
            m2.set_policy("fakerepo", "nope", True)
        with self.assertRaises(SystemExit):
            m2.set_policy("ghost-repo", "prepare", True)

    def test_policy_flip_attribution(self):
        # M2.5d: `by` attributes the flip; worker is refused; the
        # UI default stays owner.
        from openmainty import events
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "prepare", False, by="root")
        with self.assertRaises(ValueError):
            m2.set_policy("fakerepo", "prepare", True, by="worker")
        actors = [e.get("actor")
                  for e in events.list_events("fakerepo")]
        self.assertIn("owner", actors)
        self.assertIn("root", actors)
        self.assertNotIn("worker", actors)

    def test_policy_release_sentence_uses_by(self):
        # M2.5d: a root off->on flip says "Root가 다시 켬".
        from openmainty import sched
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st[m2._COOLDOWN_KEY] = {"next": "2999-01-01T00:00:00+00:00"}
            sched.save(st)
        m2.set_policy("fakerepo", "prepare", True, by="root")
        titles = [e.get("title")
                  for e in events.list_events("fakerepo")]
        self.assertIn("대기 해제: Root가 다시 켬", titles)
        self.assertNotIn("대기 해제: 소유자가 다시 켬", titles)

    def test_3_guard(self):
        # Dev process (tests run from the dev tree): refuse, loudly.
        guard = m2.promoted_guard()
        self.assertFalse(guard["ok"])
        self.assertEqual(guard["reason"], m2.GUARD_MESSAGE)
        self.assertIn("검토된 앱 사본", guard["reason"])
        # Injected running sha (a promoted process): pass.
        self.assertTrue(m2.promoted_guard(_running="abc123")["ok"])
        refused = m2.refuse("fakerepo", "some reason", "prepare")
        self.assertFalse(refused["ok"])
        kinds = [e.get("kind") for e in events.list_events("fakerepo")]
        self.assertIn("m2-blocked", kinds)

    def test_4_op_lock_refuses_overlap(self):
        # Non-blocking by design (clause 3): the first holder wins,
        # the rest get OpBusy immediately — nothing ever queues behind
        # a minutes-long fastcheck.
        entered = []
        refused = []
        gate = threading.Event()

        def worker():
            try:
                with m2.op_locked("op-1", home=self.home):
                    entered.append(True)
                    gate.wait(timeout=30)
            except m2.OpBusy:
                refused.append(True)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        import time as _t
        deadline = _t.perf_counter() + 10
        while _t.perf_counter() < deadline and \
                len(entered) + len(refused) < 3:
            _t.sleep(0.01)
        self.assertEqual(len(entered), 1)
        self.assertEqual(len(refused), 2)
        gate.set()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive())
        self.assertTrue(m2.op_lock_path("op-1", self.home).is_file())
        # Sequential reuse works: released lock re-acquires.
        with m2.op_locked("op-1", home=self.home):
            pass

    def test_4b_busy_refuses_fast(self):
        # Clause 3: contention raises instead of waiting, so an HTTP
        # thread holding the lease never hangs. Must answer < 1 s.
        import time as _t
        held = threading.Event()
        released = threading.Event()

        def holder():
            with m2.op_locked("op-busy", home=self.home):
                held.set()
                released.wait(timeout=30)

        th = threading.Thread(target=holder)
        th.start()
        self.assertTrue(held.wait(timeout=10))

        from openmainty import batches

        @m2.op_guarded(lambda **_: "op-busy",
                       busy=lambda op: batches.IntegrationError(
                           f"다른 확인 작업이 {op}에서 실행 중입니다", 409))
        def entry():
            return "ran"

        # paths.home() reads OPENMAINTY_HOME at call time, so the
        # entry locks inside the fixture home (no real-home writes).
        t0 = _t.perf_counter()
        with self.assertRaises(batches.IntegrationError) as ctx:
            entry()
        dt = _t.perf_counter() - t0
        self.assertLess(dt, 1.0)
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertIn("op-busy", str(ctx.exception))
        released.set()
        th.join(timeout=10)
        with self.assertRaises(ValueError):
            m2.op_lock_path("!!!", self.home)

    def test_4c_reresolve_lock_ids(self):
        from openmainty import batches
        ids = batches._reresolve_lock_ids("prep-x")
        self.assertEqual(ids, sorted(ids))
        self.assertIn("prep-x", ids)
        self.assertIn("prep-x-lockfix", ids)
        # A retry posts the derived id: no phantom double suffix.
        self.assertEqual(batches._reresolve_lock_ids("prep-x-lockfix"),
                         ["prep-x-lockfix"])
        # Import carries no op lock (M2.2 keys on evidence refs).
        self.assertIsNone(
            getattr(batches.import_batch, "__wrapped__", None))
        self.assertIsNotNone(
            getattr(batches.prepare_integration, "__wrapped__", None))

    def test_5_policy_api(self):
        from openmainty import batches, serve
        body = serve.api_m2_policy({"repo": "fakerepo"})
        self.assertEqual(body["line"], "꺼짐")
        self.assertIn("guard", body)
        body = serve.api_m2_policy({"repo": "fakerepo", "stage": "prepare",
                                    "on": True})
        self.assertTrue(body["policy"]["prepare"])
        self.assertEqual(body["line"], "준비만")
        # S14: the flip returns at once and queues the glance refresh
        # in the background — the baked line lands on poll, not inline.
        self.assertIn(body.get("refresh"), ("started", "queued"))
        deadline = time.time() + 15
        mine = []
        while time.time() < deadline:
            snap = batches.read_json(batches.summary_snapshot_path(),
                                     default=None)
            rows = (snap or {}).get("repos") or []
            mine = [r for r in rows if r.get("name") == "fakerepo"
                    and (r.get("system") or {}).get("m2", {}).get(
                        "line") == "준비만"]
            if mine:
                break
            time.sleep(0.2)
        self.assertTrue(mine, "background refresh never baked the line")
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_m2_policy({"repo": "fakerepo", "stage": "nope",
                                 "on": True})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_m2_policy({"repo": "ghost-repo"})
        self.assertEqual(ctx.exception.status, 404)

    def test_6_glance_line_wiring(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("자율 ${esc(m2info.line", src)

    def test_7_toggle_row(self):
        # Item 5: the repo view carries one row per stage with its
        # consequence, the order rule, and a flip button per live stage.
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("M2_STAGES_UI", src)
        self.assertIn("m2PolicySection", src)
        self.assertIn('jget("/api/m2/policy?repo="', src)
        self.assertIn('jpost("/api/m2/policy"', src)
        for consequence in ("자동으로 준비합니다", "1단계를 먼저 켜세요",
                            "다음 단계에서 열림"):
            self.assertIn(consequence, src)
        self.assertIn("m2info.guard_reason", src)
        self.assertIn('"m2": _m1_m2_line(name)',
                      (Path(__file__).resolve().parent.parent / "src" /
                       "openmainty" / "batches.py").read_text(
                           encoding="utf-8"))


class M2StageGateTests(unittest.TestCase):
    """M2.1b-1 carry-overs F1 (unopened stages refuse) and F2
    (the glance line shows the effective chain)."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})

    def test_f1_unopened_stages_refuse_on(self):
        # S15 opens "refresh": all five stages flip; only unknown
        # stage names refuse.
        for stage in ("prepare", "fastcheck", "batch", "rollup",
                      "refresh"):
            self.assertTrue(m2.set_policy("fakerepo", stage,
                                          True)[stage])
        with self.assertRaises(ValueError) as ctx:
            m2.set_policy("fakerepo", "nope", True)
        self.assertIn("unknown M2 stage", str(ctx.exception))
        # Turning them off is harmless; prepare still flips.
        self.assertFalse(m2.set_policy("fakerepo", "rollup",
                                       False)["rollup"])

    def test_f1_api_maps_to_400(self):
        from openmainty import serve
        # M2.3: rollup flips through the API; unknown stages map to
        # 400 and change nothing.
        body = serve.api_m2_policy({"repo": "fakerepo",
                                    "stage": "rollup", "on": True})
        self.assertTrue(body["policy"]["rollup"])
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_m2_policy({"repo": "fakerepo", "stage": "nope",
                                 "on": True})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("unknown M2 stage", ctx.exception.message)

    def test_f2_effective_chain_line(self):
        self.assertEqual(m2.effective_chain({}), [])
        self.assertEqual(m2.effective_chain({"prepare": True,
                                             "fastcheck": True}),
                         ["prepare", "fastcheck"])
        self.assertEqual(m2.effective_chain({"fastcheck": True}), [])
        line = m2.policy_line({"fastcheck": True})
        self.assertTrue(line.startswith("꺼짐"))
        self.assertIn("검증", line)
        self.assertIn("동작 안 함", line)
        line = m2.policy_line({"prepare": True, "rollup": True})
        self.assertTrue(line.startswith("준비만"))
        self.assertIn("합치기", line)


class FastcheckCountTests(unittest.TestCase):
    """M2.5a: the glance verification count, all offline."""

    def setUp(self):
        import json as _json
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        (self.home / "integration_ops").mkdir(parents=True)
        self._json = _json

    def _run(self, repo="fakerepo"):
        run = store.create(repo, "issue", "t", None, {"by": "t"})
        return run["id"]

    def _receipt(self, op, run_id, summary):
        (self.home / "integration_ops" / (op + ".json")).write_text(
            self._json.dumps({"op_id": op, "run_id": run_id}),
            encoding="utf-8")
        (self.home / "integration_ops"
         / (op + ".fastcheck.json")).write_text(
            self._json.dumps(summary), encoding="utf-8")

    def test_counts_direct_legacy_and_red(self):
        audience = self._run()
        legacy = self._run()
        red = self._run()
        other = self._run(repo="other")
        via = self._run()
        rode = self._run()
        self._receipt("op-a", audience,
                      {"ok": True,
                       "exercised": {"status": "direct"}})
        self._receipt("op-b", legacy, {"ok": True})
        self._receipt("op-c", red, {"ok": False})
        self._receipt("op-d", other,
                      {"ok": True,
                       "exercised": {"status": "direct"}})
        self._receipt("op-e", via,
                      {"ok": True,
                       "exercised": {"status": "via-tool",
                                    "via": {"gate": "lint"}}})
        self._receipt("op-f", rode,
                      {"ok": True,
                       "exercised": {"status": "not-run"}})
        counts = m2.recent_fastcheck_counts("fakerepo")
        # M2.5d: five buckets — via and not-run leave "확인 전".
        self.assertEqual(counts, {"verified": 4, "direct": 1,
                                  "via": 1, "notrun": 1,
                                  "unconfirmed": 1})
        self.assertEqual(
            m2.fastcheck_count_line("fakerepo"),
            "빠른 검사 통과 4건(7일) · 직접 실행 확인 1건 · "
            "도구 경유 1건 · 직접 실행 안 됨 1건 · 확인 전 1건")

    def test_empty_home_omits_zero_parts(self):
        # M2.5d: zero parts are omitted, except N.
        self.assertEqual(
            m2.fastcheck_count_line("fakerepo"),
            "빠른 검사 통과 0건(7일)")

    def test_missing_run_is_skipped_never_escapes(self):
        # M25a-F1: the op record names a run whose file is gone.
        # store.load raises SystemExit, not Exception.
        self._receipt("op-gone", "run-gone0001",
                      {"ok": True,
                       "exercised": {"status": "direct"}})
        self.assertEqual(m2.recent_fastcheck_counts("fakerepo"),
                         {"verified": 0, "direct": 0, "via": 0,
                          "notrun": 0, "unconfirmed": 0})

    def test_corrupt_receipt_is_skipped(self):
        # read_json raises SystemExit on invalid JSON too.
        run_id = self._run()
        (self.home / "integration_ops" / "op-rot.json").write_text(
            self._json.dumps({"op_id": "op-rot", "run_id": run_id}),
            encoding="utf-8")
        (self.home / "integration_ops"
         / "op-rot.fastcheck.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(m2.recent_fastcheck_counts("fakerepo"),
                         {"verified": 0, "direct": 0, "via": 0,
                          "notrun": 0, "unconfirmed": 0})

    def test_glance_renders_the_count_line(self):
        # The glance template reads sys.m2.fastcheck_line; a renamed
        # key would silently drop the count.
        dash = Path(__file__).resolve().parent.parent / "src" \
            / "openmainty" / "dashboard.html"
        self.assertIn("m2info.fastcheck_line",
                      dash.read_text(encoding="utf-8"))


class LockstepTests(unittest.TestCase):
    """M2.5b: the RN-lockstep family check over a fixture worktree."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir()
        (self.wt / "package.json").write_text(
            '{"dependencies": {"react-native": "0.86.2"}}',
            encoding="utf-8")
        (self.wt / "example").mkdir()
        (self.wt / "example" / "package.json").write_text(
            '{"dependencies": {"react": "19.1.1", '
            '"react-native": "0.86.2"}, '
            '"devDependencies": {"@react-native/metro-config": "0.86.2"}}',
            encoding="utf-8")

    def test_family_bump_alone_is_held(self):
        st = m2.lockstep_status(str(self.wt),
                                "@react-native/metro-config", "0.88.0")
        self.assertIsNotNone(st)
        self.assertIn("react-native 0.88.0 함께 옮겨야 함 (현재 0.86.2)",
                      st["reason"])
        self.assertEqual(st["workspace"], "example")
        self.assertFalse(st["unverifiable"])

    def test_same_minor_goes_alone(self):
        self.assertIsNone(m2.lockstep_status(
            str(self.wt), "@react-native/metro-config", "0.86.5"))

    def test_non_family_unaffected(self):
        self.assertIsNone(m2.lockstep_status(str(self.wt), "react",
                                             "19.3.1"))
        self.assertIsNone(m2.lockstep_status(str(self.wt), "left-pad",
                                             "1.3.0"))

    def test_undeclared_family_goes_alone(self):
        self.assertIsNone(m2.lockstep_status(
            str(self.wt), "@react-native/jest-preset", "0.88.0"))

    def test_missing_tree_is_unverifiable_hold(self):
        st = m2.lockstep_status(str(self.wt / "gone"),
                                "@react-native/metro-config", "0.88.0")
        self.assertIsNotNone(st)
        self.assertTrue(st["unverifiable"])

    def test_companions_list_same_minor(self):
        queue = [
            {"kind": "dep-update", "lane": "next",
             "dep": "@react-native/metro-config", "target": "0.88.0"},
            {"kind": "dep-update", "lane": "next",
             "dep": "@react-native/babel-preset", "target": "0.88.1"},
            {"kind": "dep-update", "lane": "next",
             "dep": "@react-native/metro-config", "target": "0.86.5"},
            {"kind": "dep-update", "lane": "main",
             "dep": "@react-native/metro-config", "target": "0.88.0"},
            {"kind": "issue-triage", "lane": "next"},
            {"lane": "next", "dep": "react", "target": "19.3.1"},
        ]
        self.assertEqual(
            m2.lockstep_companions(queue, "next", "0.88.0"),
            ["@react-native/babel-preset 0.88.1",
             "@react-native/metro-config 0.88.0"])

    def test_queue_kind_fallback(self):
        self.assertEqual(m2._queue_kind({"kind": "issue-triage"}),
                         "issue-triage")
        self.assertEqual(m2._queue_kind({"dep": "x"}), "dep-update")
        self.assertEqual(m2._queue_kind({}), "issue-triage")

    def test_empty_tree_is_unverifiable(self):
        # M2.5c-N1: an empty tree must not read the daemon cwd —
        # even when the cwd holds decoy package.jsons that would
        # clear the check.
        import json as _json
        decoy = Path(self.tmp.name) / "decoy"
        (decoy / "example").mkdir(parents=True)
        (decoy / "package.json").write_text(
            _json.dumps({"dependencies":
                         {"react-native": "0.88.0",
                          "@react-native/metro-config": "0.88.0"}}),
            encoding="utf-8")
        (decoy / "example" / "package.json").write_text(
            _json.dumps({"dependencies":
                         {"react-native": "0.88.0"}}),
            encoding="utf-8")
        old_cwd = os.getcwd()
        os.chdir(decoy)
        self.addCleanup(os.chdir, old_cwd)
        st = m2.lockstep_status("", "@react-native/metro-config",
                                "0.88.0")
        self.assertIsNotNone(st)
        self.assertTrue(st["unverifiable"])


class FcDoneWordingTests(unittest.TestCase):
    """M2.5a: the not-run timeline title names the consumers."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main",
                    "fastcheck": {"steps": [{"argv": ["echo", "x"],
                                             "gate": "unit"}],
                                  "toolchain": []}})
        # FIX-a1: the stage entry enforces the enabled prefix.
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)

    def _run_stage(self, exercised, pod=None):
        rec = {"op_id": "op-ex-1", "dep": "wds", "target": "6.1.0",
               "result": {"path": str(self.home), "head": "h" * 40,
                          "fingerprint": "f" * 64}}
        summary = {"ok": True, "steps": [{"rc": 0}], "wall_s": 3.2,
                   "exercised": exercised}
        if pod is not None:
            summary["pod_drift"] = pod
        return m2._run_fastcheck_stage(
            "fakerepo", "run-ex", "next", rec, "base-1",
            _runner=lambda *a: summary)

    def _run_stage_with_consumer(self, consumer):
        rec = {"op_id": "op-ex-1", "dep": "wds", "target": "6.1.0",
               "result": {"path": str(self.home), "head": "h" * 40,
                          "fingerprint": "f" * 64}}
        summary = {"ok": True, "steps": [{"rc": 0}], "wall_s": 3.2,
                   "exercised": {"status": "not-run",
                                 "consumer_scripts": ["example: web"]},
                   "consumer": consumer}
        return m2._run_fastcheck_stage(
            "fakerepo", "run-ex", "next", rec, "base-1",
            _runner=lambda *a: summary)

    def test_notrun_title_names_consumers(self):
        out = self._run_stage({"status": "not-run",
                               "consumer_scripts": ["example: web"]})
        self.assertTrue(out["ran"])
        titles = [e.get("title")
                  for e in events.list_events("fakerepo")]
        self.assertIn("빠른 검사 통과 — 이 패키지를 직접 실행한 단계 없음",
                      titles)

    def test_ci_consumers_title_names_pr_ci(self):
        # M2.8a1: a not-run receipt with CI consumers says the
        # package runs in PR CI, not locally.
        out = self._run_stage({"status": "not-run",
                               "consumer_scripts": [],
                               "ci_consumers": ["ci-web.yml",
                                                "ci-android.yml"]})
        self.assertTrue(out["ran"])
        titles = [e.get("title")
                  for e in events.list_events("fakerepo")]
        self.assertIn(
            "로컬 검사에서 실행되지 않음 — PR CI에서만 실행됨"
            "(ci-web.yml, ci-android.yml) · PR CI 결과로 확인",
            titles)
        details = [e.get("detail")
                   for e in events.list_events("fakerepo")]
        self.assertTrue(any("PR CI 결과로 확인" in (d or "")
                            for d in details))

    def test_consumer_pass_renames_title(self):
        # M2.15: a passing consumer step replaces the not-run
        # title with its own label.
        out = self._run_stage_with_consumer(
            {"status": "pass", "script": "example: build:web",
             "label": "소비 스크립트 build:web 통과 (59초)"})
        self.assertTrue(out["ran"])
        titles = [e.get("title")
                  for e in events.list_events("fakerepo")]
        self.assertIn("소비 스크립트 build:web 통과 (59초)", titles)
        self.assertNotIn("빠른 검사 통과 — 이 패키지를 직접 실행한 단계 없음",
                         titles)

    def test_consumer_skip_keeps_notrun_title(self):
        # A clone-failure skip keeps today's not-run wording.
        out = self._run_stage_with_consumer(
            {"status": "skip", "script": "example: build:web",
             "reason": "복제 실패 — 소비 스크립트 미실행 (x)"})
        self.assertTrue(out["ran"])
        titles = [e.get("title")
                  for e in events.list_events("fakerepo")]
        self.assertIn("빠른 검사 통과 — 이 패키지를 직접 실행한 단계 없음",
                      titles)

    def test_direct_and_legacy_keep_today_title(self):
        self._run_stage({"status": "direct", "direct_steps": ["lint"]})
        self._run_stage({})
        titles = [e.get("title")
                  for e in events.list_events("fakerepo")]
        self.assertEqual(titles.count("자율 검증 완료"), 1)
        # The second run reuses the fc-done key, so the legacy title
        # stays from the first; what matters is no not-run title.
        self.assertNotIn("빠른 검사 통과 — 이 패키지를 직접 실행한 단계 없음",
                         titles)

    def test_viatool_title_names_gate_as_guess(self):
        # M2.5d: a via-tool receipt says which gate loads it, as a
        # convention guess — never "자율 검증 완료".
        out = self._run_stage({"status": "via-tool",
                               "via": {"gate": "lint",
                                       "label": "lint (lint: eslint)"}})
        self.assertTrue(out["ran"])
        titles = [e.get("title")
                  for e in events.list_events("fakerepo")]
        self.assertIn("빠른 검사 통과 — lint 단계가 불러오는 "
                      "패키지(규칙 추정)", titles)
        self.assertNotIn("자율 검증 완료", titles)

    def test_pod_drift_rides_the_detail(self):
        out = self._run_stage({"status": "direct"},
                              pod={
                                  "pod": "RNVVideo", "pinned": "6.19.2",
                                  "target": "6.19.3",
                                  "label": ("iOS Podfile.lock 갱신 필요 "
                                            "(pod install) — iOS 빌드 전 "
                                            "미완성")})
        self.assertTrue(out["ran"])
        details = [e.get("detail") or ""
                   for e in events.list_events("fakerepo")]
        self.assertTrue(any("pod install" in d and "6.19.2" in d
                            for d in details))


class CorruptAncestryTests(unittest.TestCase):
    # F10: a corrupt source-prepare record (SystemExit from
    # read_json) reads as absent — the pipeline never escapes it.
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        (self.home / "integration_ops").mkdir(parents=True)

    def test_corrupt_source_prepare_reads_absent(self):
        (self.home / "integration_ops" / "src-op.json").write_text(
            "{corrupt", encoding="utf-8")
        rec = {"op": "reresolve", "derived_from": "src-op",
               "run_id": "run-1", "status": "done"}
        self.assertIsNone(m2._pipeline_prepare_rec(rec))


class DaemonM2BoundaryTests(unittest.TestCase):
    # F10: one repo's SystemExit never skips the other repos' tick.
    def setUp(self):
        from unittest import mock as _mock
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        repos.save({"name": "repo-a", "github": "o/a",
                    "baseBranch": "main"})
        repos.save({"name": "repo-b", "github": "o/b",
                    "baseBranch": "main"})
        self._mock = _mock

    def test_systemexit_contained_per_repo(self):
        from openmainty import m2 as _m2mod
        with self._mock.patch("openmainty.sched.tick",
                              return_value={}), \
                self._mock.patch.object(
                    _m2mod, "tick_repo",
                    side_effect=[SystemExit("error: invalid JSON "
                                           "in x"),
                                 {"ran": True, "reason": ""}]):
            out = sched.daemon_tick()
        self.assertFalse(out["m2"]["repo-a"]["ran"])
        self.assertIn("SystemExit", out["m2"]["repo-a"]["reason"])
        self.assertTrue(out["m2"]["repo-b"]["ran"])


class PrepareDoneTitleTests(unittest.TestCase):
    # M2.8a2: from -> to with a semver-major flag (0.x minor
    # counts as major); unknown from keeps today's title.

    def test_cases(self):
        t = m2.prepare_done_title
        self.assertEqual(
            t("expo", "57.0.11", "58.0.17"),
            "자율 준비 완료 expo 57.0.11 -> 58.0.17 (메이저)")
        self.assertEqual(
            t("expo", "57.0.11", "58.0.17", lang="en"),
            "자율 준비 완료 expo 57.0.11 -> 58.0.17 (major)")
        self.assertEqual(
            t("expo", "58.0.14", "58.0.17"),
            "자율 준비 완료 expo 58.0.14 -> 58.0.17")
        self.assertEqual(
            t("react-native", "0.86.2", "0.88.0"),
            "자율 준비 완료 react-native 0.86.2 -> 0.88.0 (메이저)")
        self.assertEqual(
            t("react-native", "0.86.2", "0.88.0", lang="en"),
            "자율 준비 완료 react-native 0.86.2 -> 0.88.0 (major)")
        self.assertEqual(
            t("expo", "", "58.0.17"),
            "자율 준비 완료 expo 58.0.17")
        self.assertEqual(
            t("expo", None, "58.0.17"),
            "자율 준비 완료 expo 58.0.17")

    def test_group_suffix(self):
        t = m2.prepare_done_title
        group = ["react-native 0.86.2->0.88.0",
                 "slider 3.0.0->4.0.0",
                 "a 1.0.0->1.1.0",
                 "b 1.0.0->1.1.0",
                 "c 1.0.0->1.1.0",
                 "d 1.0.0->1.1.0"]
        self.assertEqual(
            t("react-native", "0.86.2", "0.88.0", group=group),
            "자율 준비 완료 react-native 0.86.2 -> 0.88.0 "
            "(메이저) 외 5개 같이 올림")
        self.assertEqual(
            t("react-native", "0.86.2", "0.88.0", lang="en",
              group=group),
            "자율 준비 완료 react-native 0.86.2 -> 0.88.0 "
            "(major) plus 5 others")
        # A singleton group reads like a single derive.
        self.assertEqual(
            t("expo", "57.0.11", "58.0.17",
              group=["expo 57.0.11->58.0.17"]),
            "자율 준비 완료 expo 57.0.11 -> 58.0.17 (메이저)")
        # Unknown from keeps today's title even with a group.
        self.assertEqual(
            t("expo", "", "58.0.17", group=group),
            "자율 준비 완료 expo 58.0.17")


class PrepareTitleTickTests(unittest.TestCase):
    # M2.8a2: the tick feeds the done title from the queue item's
    # own data — installed lock version first, else the declared
    # range. A ^-range reads at its floor for the flag; unknown
    # keeps today's title.

    def _title(self, item, lang="ko"):
        return m2.prepare_done_title(
            item.get("dep") or "",
            m2._item_from_version(item),
            item.get("target") or "", lang=lang)

    def test_installed_beats_declared(self):
        self.assertEqual(
            self._title({"dep": "expo", "installed": "57.0.11",
                         "declared": "^57.0.0", "target": "58.0.17"}),
            "자율 준비 완료 expo 57.0.11 -> 58.0.17 (메이저)")

    def test_declared_range_fallback(self):
        self.assertEqual(
            self._title({"dep": "expo", "declared": "^57.0.11",
                         "target": "58.0.17"}),
            "자율 준비 완료 expo ^57.0.11 -> 58.0.17 (메이저)")

    def test_unknown_from_keeps_today_title(self):
        self.assertEqual(
            self._title({"dep": "expo", "target": "58.0.17"}),
            "자율 준비 완료 expo 58.0.17")
        self.assertEqual(m2._item_from_version({}), "")
        self.assertEqual(m2._item_from_version(None), "")

    def test_tick_title_en(self):
        self.assertEqual(
            self._title({"dep": "expo", "installed": "57.0.11",
                         "target": "58.0.17"}, lang="en"),
            "자율 준비 완료 expo 57.0.11 -> 58.0.17 (major)")


if __name__ == "__main__":
    unittest.main()
