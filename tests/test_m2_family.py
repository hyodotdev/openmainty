"""M2.6: superseded runs, release-train family prepare, cooldown honesty.

Superseded runs (older reviewed target, same dep+lane) skip once as
대체됨 — never prepared, never a block. Release-train families (same
lane, target, scan and npm scope) prepare as ONE combined candidate:
one _prepare_family call, one worktree, one lock re-resolve, one batch
with N primaries; a family with any unreviewed member waits whole.
A lone member's conflict with waiting siblings reports 가족 준비 필요,
never a block; without siblings the block counts as before.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, events, m2, repos, runner, sched, store  # noqa: E402
from openmainty.util import read_json, write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


class FamilyKeyTests(unittest.TestCase):
    def test_scope_is_family_key(self):
        self.assertEqual(m2._family_key("@commitlint/cli"),
                         "@commitlint")
        self.assertEqual(m2._family_key("@react-native/metro-config"),
                         "@react-native")

    def test_unscoped_joins_own_scope(self):
        # commitlint rides with @commitlint/*.
        self.assertEqual(m2._family_key("commitlint"), "@commitlint")
        self.assertEqual(m2._family_key("react"), "@react")

    def test_empty_has_no_family(self):
        self.assertEqual(m2._family_key(""), "")
        self.assertEqual(m2._family_key(None), "")


def _item(run_id, dep, target, lane="next", verified=True,
          created="2026-10-18T14:38:46+00:00"):
    return {"run_id": run_id, "status": "waiting-review", "lane": lane,
            "dep": dep, "target": target, "verified": verified,
            "kind": "dep-update", "created_at": created}


class SupersedeMapTests(unittest.TestCase):
    def test_older_reviewed_target_superseded(self):
        got = m2._supersede_map([
            _item("run-old", "commitlint", "21.2.3"),
            _item("run-new", "commitlint", "21.2.4")])
        self.assertEqual(got, {"run-old": "21.2.4"})

    def test_newest_never_listed(self):
        got = m2._supersede_map([
            _item("run-old", "commitlint", "21.2.3"),
            _item("run-new", "commitlint", "21.2.4")])
        self.assertNotIn("run-new", got)

    def test_unverified_never_supersedes(self):
        got = m2._supersede_map([
            _item("run-old", "commitlint", "21.2.3"),
            _item("run-new", "commitlint", "21.2.4", verified=False)])
        self.assertEqual(got, {})

    def test_non_semver_never_supersedes(self):
        got = m2._supersede_map([
            _item("run-old", "commitlint", "main"),
            _item("run-new", "commitlint", "21.2.4")])
        self.assertEqual(got, {})

    def test_other_lanes_do_not_group(self):
        got = m2._supersede_map([
            _item("run-old", "commitlint", "21.2.3", lane="main"),
            _item("run-new", "commitlint", "21.2.4", lane="next")])
        self.assertEqual(got, {})


class DriverFixture(unittest.TestCase):
    SCAN = "2026-10-18T14:38:46+00:00"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        self.prepared = []
        self.families = []
        self.family_retries = []

    def _mk_run(self, dep, target, lane="next", verified=True,
                created=SCAN):
        run = store.create("fakerepo", "issue", "t", None, {"by": "t"})
        rid = run["id"]
        st = store.load(rid)
        st["dep"] = dep
        st["target"] = target
        st["lane"] = lane
        store.save(st)
        with sched.locked("fakerepo"):
            qst = sched.load("fakerepo")
            qst.setdefault("queue", []).append(
                {"run_id": rid, "status": "waiting-review", "lane": lane,
                 "dep": dep, "target": target, "verified": verified,
                 "kind": "dep-update", "created_at": created})
            sched.save(qst)
        return rid

    def _fake_prepare(self, run_id, base_id, origin="m2"):
        self.prepared.append(run_id)
        return {"op": "prepare",
                "op_id": f"prep-{run_id}-base-1", "run_id": run_id,
                "status": "done",
                "result": {"branch": "codex/fresh", "head": "c" * 40}}

    def _fake_family(self, repo, lane, base_id, members, origin="m2",
                     retry=False):
        self.families.append([m["run_id"] for m in members])
        self.family_retries.append(bool(retry))
        primary = members[0]["run_id"]
        return {"ok": True,
                "rec": {"op": "prepare",
                        "op_id": f"prep-{primary}-base-1",
                        "run_id": primary, "status": "done",
                        "dep": members[0]["dep"],
                        "target": members[0]["target"],
                        "family": {"role": "primary"},
                        "result": {"branch": "codex/family-x",
                                   "head": "d" * 40,
                                   "fingerprint": "e" * 64}},
                "note": "함께 준비: %s %s" % (
                    ", ".join(m["dep"] for m in members),
                    members[0]["target"]),
                "primary_op": f"prep-{primary}-base-1"}

    def _kinds(self):
        return [e.get("kind") for e in events.list_events("fakerepo")]

    def _titles(self):
        return [e.get("title") for e in events.list_events("fakerepo")]


class SupersedeDriverTests(DriverFixture):
    def test_older_target_skipped_newer_prepares(self):
        old = self._mk_run("commitlint", "21.2.3",
                           created="2026-10-17T17:10:48+00:00")
        new = self._mk_run("commitlint", "21.2.4")
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare)
        self.assertEqual(self.prepared, [new])
        skipped = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn("대체됨 (21.2.4 있음)", skipped[old])
        self.assertNotIn("m2-blocked", self._kinds())
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})


class SupersedeLiveTests(DriverFixture):
    def test_expo_train_only_newest_prepares(self):
        # Live 15:29:13Z: expo 58.0.15/58.0.16/58.0.17 all reviewed
        # on next; oldest-first prepared 58.0.15 and the target check
        # rightly refused (^58.0.15 resolved to 58.0.17). Only the
        # newest may prepare; the older two skip, never a block.
        old = self._mk_run("expo", "58.0.15",
                           created="2026-10-17T17:10:48+00:00")
        mid = self._mk_run("expo", "58.0.16",
                           created="2026-10-18T14:38:46+00:00")
        new = self._mk_run("expo", "58.0.17")
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare)
        self.assertEqual(self.prepared, [new])
        skipped = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn("대체됨 (58.0.17 있음)", skipped[old])
        self.assertIn("대체됨 (58.0.17 있음)", skipped[mid])
        self.assertNotIn("m2-blocked", self._kinds())
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})


class SupersedeWordingTests(DriverFixture):
    def test_grouped_title_carries_no_version(self):
        # M2.6b item 3: two superseded runs with different newers
        # share one title without either version; per-run
        # "<dep> <old> -> <new>" goes in the detail.
        self._mk_run("dep-a", "1.0",
                     created="2026-10-17T17:10:48+00:00")
        self._mk_run("dep-a", "2.0")
        self._mk_run("dep-b", "3.0",
                     created="2026-10-17T17:10:48+00:00")
        self._mk_run("dep-b", "4.0")
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=self._fake_prepare)
        titles = [e.get("title") or ""
                  for e in events.list_events("fakerepo")
                  if e.get("kind") == "m2-skip"]
        sup = [t for t in titles if t.startswith("대체됨 ")]
        self.assertEqual(len(sup), 1)
        self.assertIn("대체됨 2건 (더 새 버전이 대기 중)", sup[0])
        self.assertNotIn("2.0", sup[0])
        self.assertNotIn("4.0", sup[0])
        details = [e.get("detail") or ""
                   for e in events.list_events("fakerepo")
                   if e.get("kind") == "m2-skip"
                   and (e.get("title") or "").startswith("대체됨 ")]
        self.assertEqual(len(details), 1)
        self.assertIn("dep-a 1.0 -> 2.0", details[0])
        self.assertIn("dep-b 3.0 -> 4.0", details[0])


class FamilyStaleRefreshTests(DriverFixture):
    def _stale_family(self):
        ids = [self._mk_run(dep, "8.72.0") for dep in
               ("@typescript-eslint/eslint-plugin",
                "@typescript-eslint/parser")]
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)
        return ids

    def _stale_out(self):
        return {"ok": False,
                "error": "기준이 같은 의존성 선언을 이미 변경함"
                         "(^8.71.2) — 후보를 현재 기준에서 새로 "
                         "만들어야 함; new worktree removed",
                "rec": {"op_id": "prep-x-base-1"},
                "primary_op": "prep-x-base-1"}

    def test_stale_family_queues_refresh_without_count(self):
        self._stale_family()
        real_q = m2._maybe_queue_refresh
        m2._maybe_queue_refresh = (
            lambda *a, **k: "queued")
        self.addCleanup(setattr, m2, "_maybe_queue_refresh", real_q)
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self._fake_prepare,
            _prepare_family=lambda *a, **k: self._stale_out())
        self.assertFalse(
            [p for p in out.get("prepared", [])])
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})

    def test_stale_family_without_refresh_counts(self):
        self._stale_family()
        out = m2.tick_repo(
            "fakerepo", _running="promoted",
            _prepare=self._fake_prepare,
            _prepare_family=lambda *a, **k: self._stale_out())
        st = sched.load("fakerepo")
        self.assertEqual((st.get("m2_consec_blocks") or {}).get(
            "next"), 1)


class FamilyDriverTests(DriverFixture):
    DEPS = ["@commitlint/cli", "@commitlint/config-conventional",
            "commitlint"]

    def _mk_family(self, verified_all=True):
        return [self._mk_run(dep, "21.2.4", verified=verified_all)
                for dep in self.DEPS]

    def test_family_prepares_as_one(self):
        ids = self._mk_family()
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare,
                           _prepare_family=self._fake_family)
        # One combined call — never three single prepares. The
        # driver orders members oldest-first (run_id tie-break).
        self.assertEqual(self.families, [sorted(ids)])
        self.assertEqual(self.prepared, [])
        self.assertTrue(any(
            t.startswith("함께 준비: ") for t in self._titles()))
        self.assertNotIn("m2-blocked", self._kinds())

    def test_unreviewed_member_holds_whole_family(self):
        ids = self._mk_family()
        with sched.locked("fakerepo"):
            qst = sched.load("fakerepo")
            for item in qst.get("queue") or []:
                if item.get("run_id") == ids[2]:
                    item["verified"] = False
            sched.save(qst)
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare,
                           _prepare_family=self._fake_family)
        self.assertEqual(self.families, [])
        self.assertEqual(self.prepared, [])
        skipped = {s["run"]: s["reason"] for s in out["skipped"]}
        for rid in ids[:2]:
            self.assertIn("가족 준비 대기: 리뷰 남음 1건",
                          skipped[rid])
        self.assertNotIn("m2-blocked", self._kinds())

    def test_lone_conflict_with_siblings_defers_no_block(self):
        # The sibling rides an older scan, so the lone member prepares
        # alone; its conflict defers to the family path.
        lone = self._mk_run("@commitlint/cli", "21.2.4")
        self._mk_run("@commitlint/config-conventional", "21.2.4",
                     created="2026-10-17T17:10:48+00:00")
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)

        def blocked_prepare(run_id, base_id, origin="m2"):
            self.prepared.append(run_id)
            if run_id != lone:
                return {"op": "prepare",
                        "op_id": f"prep-{run_id}-base-1",
                        "run_id": run_id, "status": "done",
                        "dep": "@commitlint/config-conventional",
                        "target": "21.2.4",
                        "result": {"branch": "codex/y",
                                   "head": "c" * 40}}
            return {"op": "prepare",
                    "op_id": f"prep-{run_id}-base-1", "run_id": run_id,
                    "status": "blocked", "dep": "@commitlint/cli",
                    "target": "21.2.4",
                    "error": "cherry-pick conflicted or failed: lock",
                    "result": {"branch": "codex/x", "head": ""}}
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=blocked_prepare,
                           _prepare_family=self._fake_family)
        skipped = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn("가족 준비 필요", skipped[lone])
        # Deferred, never counted: no block event, no consec count.
        self.assertNotIn("m2-blocked", self._kinds())
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})

    def test_refusal_reaching_into_sibling_lines_defers(self):
        # M2.6b item 2: a lone single refused for touching more
        # than its own range line defers to the family path when a
        # queued sibling shares the scope — never a counted block
        # (the live ts-eslint shape).
        lone = self._mk_run("@typescript-eslint/eslint-plugin",
                            "8.72.0",
                            created="2026-10-19T17:44:50+00:00")
        self._mk_run("@typescript-eslint/parser", "8.72.0",
                     created="2026-10-19T17:45:50+00:00")
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)

        def refusing_prepare(run_id, base_id, origin="m2"):
            self.prepared.append(run_id)
            if run_id != lone:
                return {"op": "prepare",
                        "op_id": f"prep-{run_id}-base-1",
                        "run_id": run_id, "status": "done",
                        "dep": "@typescript-eslint/parser",
                        "target": "8.72.0",
                        "result": {"branch": "codex/y",
                                   "head": "c" * 40}}
            raise batches.IntegrationError(
                "candidate manifest package.json touches more than "
                "@typescript-eslint/eslint-plugin's own range line; "
                "refusing")
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=refusing_prepare,
                           _prepare_family=self._fake_family)
        skipped = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn("가족 준비 필요", skipped[lone])
        self.assertNotIn("m2-blocked", self._kinds())
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})

    def test_blocked_single_rejoins_train_with_retry(self):
        # Live ts-eslint split (16:30Z + 17:01Z): the plugin's solo
        # attempt left a blocked record, and the parser would
        # prepare alone next tick and block again. Instead the
        # train reunites: one family call with retry, the old
        # blocked record marked superseded_by, no solo prepare
        # and no new counted block.
        a = self._mk_run("@typescript-eslint/eslint-plugin",
                         "8.72.0",
                         created="2026-10-19T17:44:50+00:00")
        b = self._mk_run("@typescript-eslint/parser", "8.72.0",
                         created="2026-10-19T17:44:50+00:00")
        op_id = f"prep-{a}-base-1"
        self.op_recs[a] = {
            "op": "prepare", "op_id": op_id, "run_id": a,
            "status": "blocked", "origin": "m2",
            "dep": "@typescript-eslint/eslint-plugin",
            "target": "8.72.0",
            "error": "candidate manifest package.json touches more "
                     "than @typescript-eslint/eslint-plugin's own "
                     "range line; refusing",
            "result": {"branch": "codex/x", "head": ""}}
        op_path = (Path(os.environ["OPENMAINTY_HOME"])
                   / "integration_ops" / f"{op_id}.json")
        op_path.write_text(json.dumps(self.op_recs[a]),
                           encoding="utf-8")
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare,
                           _prepare_family=self._fake_family)
        self.assertEqual([sorted(f) for f in self.families],
                         [sorted([a, b])])
        self.assertEqual(self.family_retries, [True])
        self.assertEqual(self.prepared, [])
        self.assertNotIn("m2-blocked", self._kinds())
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})
        marked = json.loads(op_path.read_text(encoding="utf-8"))
        self.assertEqual(marked.get("superseded_by"),
                         f"prep-{self.families[0][0]}-base-1")
        self.assertTrue(any(
            t.startswith("가족으로 다시 시도: ") for t in self._titles()))

    def test_two_blocked_singles_reunite_exactly_once(self):
        # M2.6b2(a): two blocked SINGLE records of a fully-reviewed
        # train become exactly one family attempt with retry — one
        # call, both members, old records marked, no solo prepares.
        a = self._mk_run("@typescript-eslint/eslint-plugin",
                         "8.72.0",
                         created="2026-10-19T17:44:50+00:00")
        b = self._mk_run("@typescript-eslint/parser", "8.72.0",
                         created="2026-10-19T17:44:50+00:00")
        ops_home = Path(os.environ["OPENMAINTY_HOME"]) / \
            "integration_ops"
        for rid, dep in ((a, "@typescript-eslint/eslint-plugin"),
                         (b, "@typescript-eslint/parser")):
            rec = {"op": "prepare",
                   "op_id": f"prep-{rid}-base-1", "run_id": rid,
                   "status": "blocked", "origin": "m2", "dep": dep,
                   "target": "8.72.0", "error": "lock conflict",
                   "result": {"branch": "codex/x", "head": ""}}
            self.op_recs[rid] = rec
            (ops_home / f"prep-{rid}-base-1.json").write_text(
                json.dumps(rec), encoding="utf-8")
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare,
                           _prepare_family=self._fake_family)
        self.assertEqual([sorted(f) for f in self.families],
                         [sorted([a, b])])
        self.assertEqual(self.family_retries, [True])
        self.assertEqual(self.prepared, [])
        self.assertNotIn("m2-blocked", self._kinds())
        for rid in (a, b):
            marked = json.loads((ops_home / f"prep-{rid}-base-1.json")
                                .read_text(encoding="utf-8"))
            if rid == self.families[0][0]:
                continue
            self.assertEqual(marked.get("superseded_by"),
                             f"prep-{self.families[0][0]}-base-1")

    def test_blocked_family_never_auto_retries(self):
        # M2.6b2(b): a blocked family attempt waits for the explicit
        # retry — three ticks start no new attempt and count no new
        # block, with one grouped note.
        a = self._mk_run("@typescript-eslint/eslint-plugin",
                         "8.72.0",
                         created="2026-10-19T17:44:50+00:00")
        b = self._mk_run("@typescript-eslint/parser", "8.72.0",
                         created="2026-10-19T17:44:50+00:00")
        self.op_recs[a] = {
            "op": "prepare", "op_id": f"prep-{a}-base-1",
            "run_id": a, "status": "blocked", "origin": "m2",
            "family": {"role": "primary", "members": [a, b]},
            "result": {"branch": "codex/fam", "head": ""}}
        self.op_recs[b] = {
            "op": "prepare", "op_id": f"prep-{b}-base-1",
            "run_id": b, "status": "blocked", "origin": "m2",
            "family": {"role": "member"},
            "result": {"branch": "codex/fam", "head": ""}}
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_max_prepare_per_tick"] = 5
            sched.save(st)
        for _ in range(3):
            out = m2.tick_repo(
                "fakerepo", _running="promoted",
                _prepare=self._fake_prepare,
                _prepare_family=self._fake_family)
        self.assertEqual(self.families, [])
        self.assertEqual(self.prepared, [])
        self.assertNotIn("m2-blocked", self._kinds())
        st = sched.load("fakerepo")
        self.assertEqual(st.get("m2_consec_blocks") or {}, {})
        skipped = {s["run"]: s["reason"] for s in out["skipped"]}
        self.assertIn("가족 준비 막힘", skipped[a])
        self.assertIn("가족으로 다시 시도", skipped[a])

    def test_lone_conflict_without_siblings_still_blocks(self):
        lone = self._mk_run("left-pad", "2.0.0")

        def blocked_prepare(run_id, base_id, origin="m2"):
            return {"op": "prepare",
                    "op_id": f"prep-{run_id}-base-1", "run_id": run_id,
                    "status": "blocked", "dep": "left-pad",
                    "target": "2.0.0",
                    "error": "cherry-pick conflicted or failed: lock",
                    "result": {"branch": "codex/x", "head": ""}}
        m2.tick_repo("fakerepo", _running="promoted",
                     _prepare=blocked_prepare,
                     _prepare_family=self._fake_family)
        self.assertIn("m2-blocked", self._kinds())
        st = sched.load("fakerepo")
        self.assertEqual((st.get("m2_consec_blocks") or {}).get("next"),
                         1)


FAMILY_DEPS = ["@commitlint/cli", "@commitlint/config-conventional",
               "commitlint"]
FAMILY_TARGET = "21.2.4"
FAMILY_OLD = "^21.2.3"
FAMILY_NEW = "^21.2.4"

FAMILY_META = '''__metadata:
  version: 8
  cacheKey: 8

'''


def _family_stanza(dep, rng, ver):
    return ('"%s@%s":\n  version "%s"\n  resolved '
            '"https://registry.yarnpkg.com/%s/-/%s-%s.tgz#xxx"\n\n'
            % (dep, rng, ver, dep, dep.split("/")[-1], ver))


def _family_lock(spec):
    # spec: {dep: (range, version)}
    return (FAMILY_META + "".join(
        _family_stanza(dep, rng, ver)
        for dep, (rng, ver) in spec.items()))


def _family_pkg(rng_by_dep):
    return json.dumps({"devDependencies": dict(rng_by_dep)},
                      indent=2) + "\n"


# Fake berry yarn for families: --version, config get, and install
# --mode=update-lockfile only (scripts must be off). Per-dep target
# versions arrive via files next to the shim (cfg/<dep>) — never via
# env, because the regen runs scrubbed. Every install appends one
# line to cfg/CALLS, so tests prove exactly one re-resolve ran.
FAMILY_SHIM = '''#!/bin/sh
CFG="$(dirname "$0")/cfg"
if [ "$1" = "--version" ]; then echo "4.18.0"; exit 0; fi
if [ "$1" = "config" ]; then echo "https://registry.yarnpkg.com"; exit 0; fi
if [ "$1" != "install" ]; then
  echo "fake-yarn: unsupported argv $*" >&2; exit 2
fi
has_mode=0
for a in "$@"; do
  [ "$a" = "--mode=update-lockfile" ] && has_mode=1
done
[ "$has_mode" = "1" ] || {
  echo "fake-yarn: missing --mode=update-lockfile" >&2; exit 3; }
[ "$YARN_ENABLE_SCRIPTS" = "0" ] || {
  echo "fake-yarn: scripts not disabled" >&2; exit 3; }
echo install >> "$CFG/CALLS"
python3 - "$CFG" <<'PYEOF'
import json
import os
import sys
cfg = sys.argv[1]
pkg = json.load(open("package.json", encoding="utf-8"))
deps = dict(pkg.get("dependencies", {}))
deps.update(pkg.get("devDependencies", {}))
text = open("yarn.lock", encoding="utf-8").read()
for dep, rng in deps.items():
    try:
        ver = open(os.path.join(cfg,
                                dep.replace("/", "__"))).read().strip()
    except OSError:
        continue
    if not ver:
        continue
    chunks = text.split("\\n\\n")
    out = []
    for ch in chunks:
        lines = ch.split("\\n")
        head = lines[0].strip().rstrip(":").strip('"') if lines else ""
        descs = [d.strip().strip('"') for d in head.split(",")]
        if any(d.startswith(dep + "@") for d in descs):
            lines[0] = lines[0].replace(
                descs[0][len(dep) + 1:], rng, 1)
            for i, ln in enumerate(lines):
                if ln.strip().startswith('version "'):
                    lines[i] = '  version "%s"' % ver
                    break
            out.append("\\n".join(lines))
        else:
            out.append(ch)
    text = "\\n\\n".join(out)
open("yarn.lock", "w", encoding="utf-8").write(text)
PYEOF
exit $?
'''


class FamilyPrepareMixin(unittest.TestCase):
    """Origin @ c0; one member branch per dep; base @ c0 or @ c2.

    MODE "clean": base stays at c0, member commits replay cleanly.
    MODE "lockconflict": base moves to c2 with lock-only bumps, so
    every member conflicts in yarn.lock only.
    MODE "nonlock": base moves README.txt to c2 and the first member
    moves it differently — a non-lock conflict naming that member.
    """

    MODE = "clean"

    def _pkg(self, new_dep=None):
        rngs = {dep: FAMILY_OLD for dep in FAMILY_DEPS}
        if new_dep is not None:
            rngs[new_dep] = FAMILY_NEW
        return _family_pkg(rngs)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        self.old_path = os.environ.get("PATH", "")
        bindir = self.root / "bin"
        bindir.mkdir()
        shim = bindir / "yarn"
        shim.write_text(FAMILY_SHIM, encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = str(bindir) + ":" + self.old_path
        self.addCleanup(os.environ.__setitem__, "PATH", self.old_path)
        self._disk_patcher = mock.patch.object(
            batches, "_regen_disk_ok",
            return_value=(True, "픽스처 99.0GB"))
        self._disk_patcher.start()
        self.addCleanup(self._disk_patcher.stop)
        self.cfgdir = bindir / "cfg"
        self.cfgdir.mkdir(exist_ok=True)
        for dep in FAMILY_DEPS:
            (self.cfgdir / dep.replace("/", "__")).write_text(
                FAMILY_TARGET, encoding="utf-8")
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "package.json").write_text(
            self._pkg(), encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            _family_lock({dep: (FAMILY_OLD, "21.2.3")
                          for dep in FAMILY_DEPS}),
            encoding="utf-8")
        (self.origin / "README.txt").write_text("origin\n",
                                                encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "c0", cwd=self.origin)
        self.c0 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        self.tips = {}
        for pos, dep in enumerate(FAMILY_DEPS):
            git("checkout", "-q", "main", cwd=self.origin)
            git("checkout", "-qb", f"m{pos}", cwd=self.origin)
            (self.origin / "package.json").write_text(
                self._pkg(dep), encoding="utf-8")
            (self.origin / "yarn.lock").write_text(
                _family_lock({
                    d: ((FAMILY_NEW, FAMILY_TARGET)
                        if d == dep else (FAMILY_OLD, "21.2.3"))
                    for d in FAMILY_DEPS}),
                encoding="utf-8")
            if self.MODE == "nonlock" and pos == 0:
                (self.origin / "README.txt").write_text(
                    "cand\n", encoding="utf-8")
            git("add", "-A", cwd=self.origin)
            git("commit", "-qm", f"candidate {dep} -> {FAMILY_TARGET}",
                cwd=self.origin)
            self.tips[dep] = git("rev-parse", "HEAD",
                                 cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        if self.MODE == "lockconflict":
            (self.origin / "yarn.lock").write_text(
                _family_lock({dep: (FAMILY_OLD, "21.2.3.1")
                              for dep in FAMILY_DEPS}),
                encoding="utf-8")
            git("add", "-A", cwd=self.origin)
            git("commit", "-qm", "base moves lock", cwd=self.origin)
        if self.MODE == "nonlock":
            (self.origin / "README.txt").write_text("base\n",
                                                    encoding="utf-8")
            git("add", "-A", cwd=self.origin)
            git("commit", "-qm", "base moves readme", cwd=self.origin)
        self.base_head = git("rev-parse", "HEAD",
                             cwd=self.origin).strip()
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.base_head, cwd=self.origin)
        snap = runner.source_snapshot(self.base_wt)
        self.base_snap = {"head": snap["head"],
                          "fingerprint": snap["fingerprint"]}
        self.run_ids = []
        for dep in FAMILY_DEPS:
            run_work = self.root / "home" / "runs" / dep.replace(
                "/", "_") / "work"
            git("worktree", "add", "--detach", str(run_work),
                self.tips[dep], cwd=self.origin)
            # The run worktree must live where the store looks.
            state = store.create("fakerepo", f"dep-{dep}",
                                 f"chore(deps): {dep} -> {FAMILY_TARGET}",
                                 self.c0, {})
            rid = f"run-fam-{dep.replace('/', '-').replace('@', '')}"
            state["id"] = rid
            state["dep"] = dep
            state["target"] = FAMILY_TARGET
            state["lane"] = "next"
            state["branch"] = f"codex/next/{dep}-2124".replace("/", "-")
            csnap = runner.source_snapshot(run_work)
            state["workCommit"] = self.tips[dep]
            state["workTreeFingerprint"] = csnap["fingerprint"]
            state["verifiedCommits"] = {"review": self.tips[dep],
                                        "e2e": self.tips[dep]}
            state["verifiedFingerprints"] = {
                "review": csnap["fingerprint"],
                "e2e": csnap["fingerprint"]}
            state["steps"] = {"review": {"status": "done",
                                         "exit_code": 0},
                              "e2e": {"status": "done", "exit_code": 0}}
            store.save(state)
            # Move the worktree into the store-owned path.
            owned = store.run_dir(rid) / "work"
            owned.parent.mkdir(parents=True, exist_ok=True)
            if owned.exists():
                import shutil
                shutil.rmtree(str(owned))
            git("worktree", "remove", "--force", str(run_work),
                cwd=self.origin)
            git("worktree", "add", "--detach", str(owned),
                self.tips[dep], cwd=self.origin)
            fresh = runner.source_snapshot(owned)
            st2 = store.load(rid)
            st2["workTreeFingerprint"] = fresh["fingerprint"]
            st2["verifiedFingerprints"] = {
                "review": fresh["fingerprint"],
                "e2e": fresh["fingerprint"]}
            store.save(st2)
            with sched.locked("fakerepo"):
                qst = sched.load("fakerepo")
                qst.setdefault("queue", []).append(
                    {"run_id": rid, "status": "waiting-review",
                     "lane": "next", "dep": dep,
                     "target": FAMILY_TARGET, "verified": True,
                     "kind": "dep-update",
                     "created_at": DriverFixture.SCAN,
                     "branch": st2["branch"]})
                sched.save(qst)
            self.run_ids.append(rid)
        self._write_ready_base()
        self.members = [{"run_id": rid, "dep": dep,
                         "target": FAMILY_TARGET}
                        for rid, dep in zip(self.run_ids, FAMILY_DEPS)]

    def _write_ready_base(self):
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        base_run_id = "run-fambase0"
        base_run_work = store.run_dir(base_run_id) / "work"
        base_run_work.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(base_run_work),
            self.base_head, cwd=self.origin)
        base_run_snap = runner.source_snapshot(base_run_work)
        bstate = store.create("fakerepo", "dep-base-1.0.0",
                              "chore(deps): base 0.9.9 -> 1.0.0",
                              self.base_head, {})
        bstate["id"] = base_run_id
        bstate["dep"] = "base-dep"
        bstate["target"] = "1.0.0"
        bstate["lane"] = "next"
        bstate["branch"] = "codex/next/base-1-0-0"
        bstate["workCommit"] = self.base_head
        bstate["workTreeFingerprint"] = base_run_snap["fingerprint"]
        bstate["verifiedCommits"] = {"review": self.base_head,
                                     "e2e": self.base_head}
        bstate["verifiedFingerprints"] = {
            "review": base_run_snap["fingerprint"],
            "e2e": base_run_snap["fingerprint"]}
        bstate["steps"] = {"review": {"status": "done", "exit_code": 0},
                           "e2e": {"status": "done", "exit_code": 0}}
        store.save(bstate)
        ev_dir = batches.evidence_dir("fam-base")
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batch_file("fam-base"), {
            "schema": 3, "id": "fam-base", "repo": "fakerepo",
            "target_branch": "next", "primaries": [{
                "dep": "base-dep", "target": "1.0.0", "lane": "next",
                "run_id": base_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(base_run_work),
                           "head": self.base_head,
                           "fingerprint":
                           base_run_snap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "main",
                       "base_head": self.base_head},
            "integrations": [{"lane": "next",
                              "worktree": str(self.base_wt),
                              "head": self.base_head,
                              "fingerprint":
                              self.base_snap["fingerprint"],
                              "status": "integration-testing",
                              "gates": [{
                                  "key": "unit", "status": "pass",
                                  "inputs": {
                                      "head": self.base_head,
                                      "fingerprint":
                                      self.base_snap["fingerprint"]},
                                  "evidence": [{
                                      "label": "unit proof",
                                      "kind": "file", "stored": True,
                                      "stored_path": "proof.txt",
                                      "sha256": ev_sha,
                                      "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        self.batch_id = "fam-base"

    def _calls(self):
        try:
            return (self.cfgdir / "CALLS").read_text(
                encoding="utf-8").strip().splitlines()
        except OSError:
            return []

    def _assert_family_done(self, out):
        self.assertTrue(out.get("ok"), out.get("error"))
        rec = out["rec"]
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["family"]["role"], "primary")
        self.assertEqual(rec["family"]["members"], self.run_ids)
        self.assertIn("함께 준비: ", rec["family"]["note"])
        for dep in FAMILY_DEPS:
            self.assertIn(dep, rec["family"]["note"])
        result = rec["result"]
        self.assertTrue(result["branch"].startswith("codex/family-"))
        self.assertTrue(Path(result["path"]).is_dir())
        self.assertNotEqual(result["head"], self.base_head)
        # Exactly one lock re-resolve ran for the whole family.
        self.assertEqual(self._calls(), ["install"])
        tree = Path(result["path"])
        pkg = json.loads((tree / "package.json").read_text(
            encoding="utf-8"))
        for dep in FAMILY_DEPS:
            self.assertEqual(pkg["devDependencies"][dep], FAMILY_NEW)
        lock = (tree / "yarn.lock").read_text(encoding="utf-8")
        for dep in FAMILY_DEPS:
            self.assertIn('"%s@%s":' % (dep, FAMILY_NEW), lock)
            self.assertIn('version "%s"' % FAMILY_TARGET, lock)
        # One commit on top of the base.
        count = git("rev-list", "--count",
                    f"{self.base_head}..{result['head']}",
                    cwd=self.origin).strip()
        self.assertEqual(count, "1")
        # Member records ride the same tree, role member, same head.
        for rid, dep in zip(self.run_ids[1:], FAMILY_DEPS[1:]):
            mop = "prep-%s-%s" % (rid, self.batch_id)
            mrec = read_json(batches.op_file(mop), default=None)
            self.assertIsNotNone(mrec)
            self.assertEqual(mrec["status"], "done")
            self.assertEqual(mrec["family"]["role"], "member")
            self.assertEqual(mrec["family"]["primary"],
                             out["primary_op"])
            self.assertEqual(mrec["result"]["head"], result["head"])
            self.assertEqual(mrec["dep"], dep)
        return rec


class FamilyPrepareCleanTests(FamilyPrepareMixin):
    MODE = "clean"

    def test_clean_family_done(self):
        out = batches.prepare_family_integration(
            "fakerepo", "next", self.batch_id, self.members,
            origin="m2")
        self._assert_family_done(out)

    def test_batch_registers_three_primaries(self):
        out = batches.prepare_family_integration(
            "fakerepo", "next", self.batch_id, self.members,
            origin="m2")
        rec = out["rec"]
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        log = self.root / "step.log"
        log.write_text("ok\n", encoding="utf-8")
        fc = {"ok": True, "steps": [{"rc": 0, "argv": ["echo", "x"],
                                     "log": str(log)}]}
        profile = {"steps": [{"argv": ["echo", "x"], "gate": "unit"}]}
        seen = []
        res = m2._run_batch_stage(
            "fakerepo", self.run_ids[0], "next", rec, self.batch_id,
            fc, _import=lambda m: seen.append(m) or {"id": m["id"]},
            extra_primaries=[
                {"run_id": rid, "dep": dep, "target": FAMILY_TARGET}
                for rid, dep in zip(self.run_ids[1:], FAMILY_DEPS[1:])])
        self.assertTrue(res.get("ran"), res.get("reason"))
        manifest = seen[0]
        self.assertEqual(len(manifest["primaries"]), 3)
        self.assertEqual([p["run_id"] for p in manifest["primaries"]],
                         self.run_ids)
        self.assertIn("함께 준비: ", manifest["family"]["note"])
        for rid, dep in zip(self.run_ids, FAMILY_DEPS):
            prim = [p for p in manifest["primaries"]
                    if p["run_id"] == rid][0]
            self.assertEqual(prim["dep"], dep)
            st = store.load(rid)
            self.assertEqual(prim["source"]["head"],
                             st["workCommit"])

    def test_single_retry_of_family_primary_refused(self):
        out = batches.prepare_family_integration(
            "fakerepo", "next", self.batch_id, self.members,
            origin="m2")
        self.assertTrue(out.get("ok"))
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.prepare_integration(self.run_ids[0], self.batch_id,
                                        retry=True)
        self.assertIn("family prepare", str(ctx.exception))


class FamilyPrepareLockConflictTests(FamilyPrepareMixin):
    MODE = "lockconflict"

    def test_lock_conflict_still_done_via_regen(self):
        out = batches.prepare_family_integration(
            "fakerepo", "next", self.batch_id, self.members,
            origin="m2")
        self._assert_family_done(out)


class FamilyPrepareNonLockTests(FamilyPrepareMixin):
    MODE = "nonlock"

    def test_nonlock_conflict_blocks_naming_member(self):
        out = batches.prepare_family_integration(
            "fakerepo", "next", self.batch_id, self.members,
            origin="m2")
        self.assertFalse(out.get("ok"))
        self.assertIn(FAMILY_DEPS[0], out["error"])
        self.assertIn("README.txt", out["error"])
        rec = out["rec"]
        self.assertEqual(rec["status"], "blocked")
        self.assertFalse(Path(rec["result"]["path"]).exists())


TS_DEPS = ["@typescript-eslint/eslint-plugin",
           "@typescript-eslint/parser"]
TS_OLD = "^8.66.0"
TS_NEW = "^8.72.0"
TS_MID = "^8.71.2"
TS_TARGET = "8.72.0"


class TsPairFixture(unittest.TestCase):
    """The live ts-eslint pair: one candidate commit touches both
    manifest lines. MODE clean: base still declares the parent
    range (fresh). MODE stale: base moved both lines to TS_MID, so
    the candidates are stale and the refusal must say so (not hide
    behind the multi-line message).
    """

    MODE = "clean"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        self.old_path = os.environ.get("PATH", "")
        bindir = self.root / "bin"
        bindir.mkdir()
        shim = bindir / "yarn"
        shim.write_text(FAMILY_SHIM, encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = str(bindir) + ":" + self.old_path
        self.addCleanup(os.environ.__setitem__, "PATH", self.old_path)
        self._disk_patcher = mock.patch.object(
            batches, "_regen_disk_ok",
            return_value=(True, "픽스처 99.0GB"))
        self._disk_patcher.start()
        self.addCleanup(self._disk_patcher.stop)
        self.cfgdir = bindir / "cfg"
        self.cfgdir.mkdir(exist_ok=True)
        for dep in TS_DEPS:
            (self.cfgdir / dep.replace("/", "__")).write_text(
                TS_TARGET, encoding="utf-8")
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "package.json").write_text(
            _family_pkg({dep: TS_OLD for dep in TS_DEPS}),
            encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            _family_lock({dep: (TS_OLD, "8.66.0")
                          for dep in TS_DEPS}),
            encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "c0", cwd=self.origin)
        self.c0 = git("rev-parse", "HEAD", cwd=self.origin).strip()
        self.tips = {}
        # The plugin candidate bumps BOTH lines (live e3a0c08).
        git("checkout", "-q", "main", cwd=self.origin)
        git("checkout", "-qb", "m-plugin", cwd=self.origin)
        (self.origin / "package.json").write_text(
            _family_pkg({dep: TS_NEW for dep in TS_DEPS}),
            encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            _family_lock({dep: (TS_NEW, TS_TARGET)
                          for dep in TS_DEPS}),
            encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate plugin+parser",
            cwd=self.origin)
        self.tips[TS_DEPS[0]] = git(
            "rev-parse", "HEAD", cwd=self.origin).strip()
        # The parser candidate bumps only its own line.
        git("checkout", "-q", "main", cwd=self.origin)
        git("checkout", "-qb", "m-parser", cwd=self.origin)
        (self.origin / "package.json").write_text(
            _family_pkg({TS_DEPS[0]: TS_OLD, TS_DEPS[1]: TS_NEW}),
            encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            _family_lock({TS_DEPS[0]: (TS_OLD, "8.66.0"),
                          TS_DEPS[1]: (TS_NEW, TS_TARGET)}),
            encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "candidate parser", cwd=self.origin)
        self.tips[TS_DEPS[1]] = git(
            "rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        if self.MODE == "stale":
            (self.origin / "package.json").write_text(
                _family_pkg({dep: TS_MID for dep in TS_DEPS}),
                encoding="utf-8")
            git("add", "-A", cwd=self.origin)
            git("commit", "-qm", "base moves both lines",
                cwd=self.origin)
        self.base_head = git("rev-parse", "HEAD",
                             cwd=self.origin).strip()
        self.base_wt = self.root / "home" / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.base_head, cwd=self.origin)
        snap = runner.source_snapshot(self.base_wt)
        self.base_snap = {"head": snap["head"],
                          "fingerprint": snap["fingerprint"]}
        self.run_ids = []
        for pos, dep in enumerate(TS_DEPS):
            state = store.create("fakerepo", f"dep-{pos}",
                                 f"chore(deps): {dep} -> {TS_TARGET}",
                                 self.c0, {})
            rid = "run-ts-%d" % pos
            state["id"] = rid
            state["dep"] = dep
            state["target"] = TS_TARGET
            state["lane"] = "next"
            state["branch"] = "codex/next/ts-%d" % pos
            state["workCommit"] = self.tips[dep]
            owned = store.run_dir(rid) / "work"
            owned.parent.mkdir(parents=True, exist_ok=True)
            git("worktree", "add", "--detach", str(owned),
                self.tips[dep], cwd=self.origin)
            fresh = runner.source_snapshot(owned)
            state["workTreeFingerprint"] = fresh["fingerprint"]
            state["verifiedCommits"] = {"review": self.tips[dep],
                                        "e2e": self.tips[dep]}
            state["verifiedFingerprints"] = {
                "review": fresh["fingerprint"],
                "e2e": fresh["fingerprint"]}
            state["steps"] = {"review": {"status": "done",
                                         "exit_code": 0},
                              "e2e": {"status": "done",
                                      "exit_code": 0}}
            store.save(state)
            with sched.locked("fakerepo"):
                qst = sched.load("fakerepo")
                qst.setdefault("queue", []).append(
                    {"run_id": rid, "status": "waiting-review",
                     "lane": "next", "dep": dep,
                     "target": TS_TARGET, "verified": True,
                     "kind": "dep-update",
                     "created_at": "2026-10-19T17:44:50+00:00",
                     "branch": state["branch"]})
                sched.save(qst)
            self.run_ids.append(rid)
        self._write_ready_base()
        self.members = [{"run_id": rid, "dep": dep,
                         "target": TS_TARGET}
                        for rid, dep in zip(self.run_ids, TS_DEPS)]

    def _write_ready_base(self):
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        base_run_id = "run-tsbase0"
        base_run_work = store.run_dir(base_run_id) / "work"
        base_run_work.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(base_run_work),
            self.base_head, cwd=self.origin)
        base_run_snap = runner.source_snapshot(base_run_work)
        bstate = store.create("fakerepo", "dep-base-1.0.0",
                              "chore(deps): base 0.9.9 -> 1.0.0",
                              self.base_head, {})
        bstate["id"] = base_run_id
        bstate["dep"] = "base-dep"
        bstate["target"] = "1.0.0"
        bstate["lane"] = "next"
        bstate["branch"] = "codex/next/base-1-0-0"
        bstate["workCommit"] = self.base_head
        bstate["workTreeFingerprint"] = base_run_snap["fingerprint"]
        bstate["verifiedCommits"] = {"review": self.base_head,
                                     "e2e": self.base_head}
        bstate["verifiedFingerprints"] = {
            "review": base_run_snap["fingerprint"],
            "e2e": base_run_snap["fingerprint"]}
        bstate["steps"] = {"review": {"status": "done", "exit_code": 0},
                           "e2e": {"status": "done", "exit_code": 0}}
        store.save(bstate)
        ev_dir = batches.evidence_dir("ts-base")
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batch_file("ts-base"), {
            "schema": 3, "id": "ts-base", "repo": "fakerepo",
            "target_branch": "next", "primaries": [{
                "dep": "base-dep", "target": "1.0.0", "lane": "next",
                "run_id": base_run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(base_run_work),
                           "head": self.base_head,
                           "fingerprint":
                           base_run_snap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.base_wt),
                       "base_branch": "main",
                       "base_head": self.base_head},
            "integrations": [{"lane": "next",
                              "worktree": str(self.base_wt),
                              "head": self.base_head,
                              "fingerprint":
                              self.base_snap["fingerprint"],
                              "status": "integration-testing",
                              "gates": [{
                                  "key": "unit", "status": "pass",
                                  "inputs": {
                                      "head": self.base_head,
                                      "fingerprint":
                                      self.base_snap["fingerprint"]},
                                  "evidence": [{
                                      "label": "unit proof",
                                      "kind": "file", "stored": True,
                                      "stored_path": "proof.txt",
                                      "sha256": ev_sha,
                                      "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        self.batch_id = "ts-base"

    def _calls(self):
        try:
            return (self.cfgdir / "CALLS").read_text(
                encoding="utf-8").strip().splitlines()
        except OSError:
            return []


class TsPairOverlapTests(TsPairFixture):
    MODE = "clean"

    def test_overlapping_pair_prepares_as_one_family(self):
        out = batches.prepare_family_integration(
            "fakerepo", "next", self.batch_id, self.members,
            origin="m2")
        self.assertTrue(out.get("ok"), out.get("error"))
        rec = out["rec"]
        tree = Path(rec["result"]["path"])
        pkg = json.loads((tree / "package.json").read_text(
            encoding="utf-8"))
        for dep in TS_DEPS:
            self.assertEqual(pkg["devDependencies"][dep], TS_NEW)
        # The parser's own line already held its intent when its
        # pick ran (the plugin put it there): recorded, not
        # refused, never a stale claim.
        entries = rec["regenerated_by"]["manifest_reapplied"]
        by_dep = {e["dep"]: e for e in entries}
        self.assertIn(TS_DEPS[1], by_dep)
        self.assertIn(TS_DEPS[1],
                      by_dep[TS_DEPS[1]].get("already_applied", []))
        self.assertEqual(self._calls(), ["install"])


class ReapplySiblingTests(DriverFixture):
    # The live ts-eslint shape at the unit level: the overlapping
    # commit touches both lines, its own pick conflicts, and the
    # fellow member's line defers to its own pick instead of
    # refusing — while a stranger's line still refuses.
    OLD = "^8.66.0"
    NEW = "^8.72.0"
    PLUGIN = "@typescript-eslint/eslint-plugin"
    PARSER = "@typescript-eslint/parser"

    def _repos(self):
        root = Path(self.tmp.name) / "reapply"
        root.mkdir()
        origin = root / "origin"
        origin.mkdir()
        git("init", "-b", "main", cwd=origin)
        git("config", "user.email", "test@localhost", cwd=origin)
        git("config", "user.name", "test", cwd=origin)
        pkg = {"devDependencies": {self.PLUGIN: self.OLD,
                                  self.PARSER: self.OLD}}
        (origin / "package.json").write_text(
            json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
        git("add", "-A", cwd=origin)
        git("commit", "-qm", "c0", cwd=origin)
        c0 = git("rev-parse", "HEAD", cwd=origin).strip()
        git("checkout", "-qb", "cand", cwd=origin)
        pkg = {"devDependencies": {self.PLUGIN: self.NEW,
                                  self.PARSER: self.NEW}}
        (origin / "package.json").write_text(
            json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
        git("add", "-A", cwd=origin)
        git("commit", "-qm", "candidate plugin+parser", cwd=origin)
        c1 = git("rev-parse", "HEAD", cwd=origin).strip()
        wt = root / "wt"
        git("worktree", "add", "--detach", str(wt), c0, cwd=origin)
        return origin, wt, c0, c1

    def _call(self, dep, siblings):
        origin, wt, c0, c1 = self._repos()
        return batches._reapply_manifest_ranges(
            worktree=str(wt), cand_worktree=str(origin),
            base_head=c0, base_commit=c0, rev_list=[c1], pos=1,
            dep=dep, manifest_paths=["package.json"],
            prior_texts={}, sibling_deps=siblings), wt

    def test_sibling_line_defers_to_own_pick(self):
        entries, wt = self._call(self.PLUGIN, (self.PARSER,))
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            (entries[0]["from"], entries[0]["to"]),
            (self.OLD, self.NEW))
        self.assertEqual(entries[0].get("deferred_to_sibling"),
                         [self.PARSER])
        pkg = json.loads((wt / "package.json").read_text(
            encoding="utf-8"))
        self.assertEqual(pkg["devDependencies"][self.PLUGIN],
                         self.NEW)
        # Never ours to apply: left for the sibling's own pick.
        self.assertEqual(pkg["devDependencies"][self.PARSER],
                         self.OLD)

    def test_stranger_line_still_refuses(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            self._call(self.PLUGIN, ())
        self.assertIn("touches more than", str(ctx.exception))


class TsPairStaleTests(TsPairFixture):
    MODE = "stale"

    def test_stale_base_reports_same_line_not_multi(self):
        out = batches.prepare_family_integration(
            "fakerepo", "next", self.batch_id, self.members,
            origin="m2")
        self.assertFalse(out.get("ok"))
        # The base moved the lines (TS_MID): the refusal names the
        # stale declaration, it must not hide behind multi-line.
        self.assertIn("기준이 같은 의존성 선언을 이미 변경함",
                      out["error"])
        self.assertIn(TS_MID, out["error"])
        self.assertNotIn("touches more than", out["error"])


class FamilyPrepareProofTests(FamilyPrepareMixin):
    MODE = "clean"

    def test_unreviewed_member_refused(self):
        st = store.load(self.run_ids[0])
        st.pop("verifiedFingerprints", None)
        store.save(st)
        with self.assertRaises(batches.IntegrationError):
            batches.prepare_family_integration(
                "fakerepo", "next", self.batch_id, self.members,
                origin="m2")


class FamilyRetryTests(FamilyPrepareMixin):
    """M2.6b: one explicit retry for a blocked train (trio fixture).

    Three blocked single-member prepare records become one done
    family op; the old records are marked superseded_by, never
    deleted; one timeline event records who retried.
    """

    MODE = "clean"

    def _block_member(self, rid, error="cherry-pick conflicted"):
        op_id = "prep-%s-%s" % (rid, self.batch_id)
        rec = {"op": "prepare", "op_id": op_id, "run_id": rid,
               "repo": "fakerepo", "batch_id": self.batch_id,
               "dep": store.load(rid)["dep"], "target": FAMILY_TARGET,
               "lane": "next", "origin": "m2",
               "candidate": {"head": "c" * 40,
                             "fingerprint": "e" * 64,
                             "branch": "codex/old",
                             "worktree": "/nonexistent"},
               "base": {"batch": self.batch_id, "lane": "next",
                        "head": self.base_head,
                        "fingerprint": self.base_snap["fingerprint"],
                        "checkout": str(self.base_wt)},
               "result": {"path": "/tmp/om66-fam-gone-%s" % rid,
                          "branch": "codex/old-%s" % rid,
                          "head": "", "fingerprint": ""},
               "gates": [{"key": "source-prepared",
                          "status": "blocked", "note": error}],
               "status": "blocked", "error": error,
               "created_at": "2026-10-22T00:00:00+00:00",
               "updated_at": "2026-10-22T00:00:00+00:00"}
        write_json(batches.op_file(op_id), rec)
        return op_id

    def _retry(self, by="owner"):
        real_base = batches.default_base
        batches.default_base = (
            lambda repo, lane, **k: {"id": self.batch_id})
        try:
            return batches.family_retry("fakerepo", self.run_ids[0],
                                        by)
        finally:
            batches.default_base = real_base

    def test_trio_blocked_becomes_one_family(self):
        for rid in self.run_ids:
            self._block_member(rid)
        # A stray blocked derived op rides along: the marking pass
        # must catch what the member overwrite does not.
        stray = "prep-%s-%s-lockfix" % (self.run_ids[1],
                                        self.batch_id)
        write_json(batches.op_file(stray),
                   {"op": "reresolve", "op_id": stray,
                    "run_id": self.run_ids[1], "repo": "fakerepo",
                    "status": "blocked", "error": "x",
                    "result": {"branch": "codex/s", "head": ""}})
        out = self._retry(by="owner")
        self.assertTrue(out.get("ok"), out.get("error"))
        self.assertFalse(out.get("reused"))
        # The train sorts oldest-first (run_id tie-break), which need
        # not match fixture creation order.
        members = [m["run_id"] for m in out["members"]]
        self.assertEqual(sorted(members), sorted(self.run_ids))
        self.assertEqual(len(members), 3)
        fam_op = out["primary_op"]
        # Primary: fresh done record with the blocked single archived.
        prec = read_json(batches.op_file(fam_op), default=None)
        self.assertEqual(prec["status"], "done")
        self.assertEqual(prec["family"]["role"], "primary")
        self.assertEqual(prec["family"]["members"], members)
        self.assertTrue(prec.get("attempts"))
        # Members: done, pointing at the family tree, old blocks
        # archived AND marked superseded_by.
        for rid in members[1:]:
            mrec = read_json(
                batches.op_file("prep-%s-%s" % (rid, self.batch_id)),
                default=None)
            self.assertEqual(mrec["status"], "done")
            self.assertEqual(mrec["result"]["head"],
                             prec["result"]["head"])
            self.assertEqual(mrec["superseded_by"], fam_op)
            self.assertTrue(mrec.get("attempts"))
        # The stray derived op was marked, never deleted.
        srec = read_json(batches.op_file(stray), default=None)
        self.assertEqual(srec["status"], "blocked")
        self.assertEqual(srec["superseded_by"], fam_op)
        # Every superseded old record is listed; the family op
        # itself is never listed as superseded.
        self.assertIn(stray, out["marked_superseded"])
        for rid in members[1:]:
            self.assertIn("prep-%s-%s" % (rid, self.batch_id),
                          out["marked_superseded"])
        self.assertNotIn(fam_op, out["marked_superseded"])
        # One timeline event names the train and the retrier.
        evs = [e for e in events.list_events("fakerepo")
               if e.get("kind") == "m2-family-retry"]
        self.assertEqual(len(evs), 1)
        self.assertIn("가족으로 다시 시도", evs[0]["title"])
        self.assertIn("owner", evs[0]["title"])
        self.assertEqual(evs[0]["actor"], "owner")

    def test_second_call_reuses_without_new_event(self):
        for rid in self.run_ids:
            self._block_member(rid)
        first = self._retry(by="root")
        self.assertTrue(first.get("ok"))
        second = self._retry(by="root")
        self.assertTrue(second.get("ok"))
        self.assertTrue(second.get("reused"))
        evs = [e for e in events.list_events("fakerepo")
               if e.get("kind") == "m2-family-retry"]
        self.assertEqual(len(evs), 1)

    def test_retry_after_blocked_family_attempt(self):
        # M2.6b2(c): a blocked family attempt stays retryable
        # through the explicit path — the driver never auto-runs
        # it, but family_retry archives and re-runs it to done.
        for rid in self.run_ids:
            self._block_member(rid)
        members = [m["run_id"] for m in batches.family_train(
            "fakerepo", self.run_ids[0])]
        primary = members[0]
        fam_op = "prep-%s-%s" % (primary, self.batch_id)
        fam = read_json(batches.op_file(fam_op), default=None)
        fam["family"] = {"role": "primary", "members": members,
                         "note": "trio first attempt"}
        fam["error"] = "family member lock conflict"
        write_json(batches.op_file(fam_op), fam)
        out = self._retry(by="owner")
        self.assertTrue(out.get("ok"), out.get("error"))
        self.assertFalse(out.get("reused"))
        prec = read_json(batches.op_file(fam_op), default=None)
        self.assertEqual(prec["status"], "done")
        self.assertEqual(prec["family"]["role"], "primary")
        self.assertTrue(prec.get("attempts"))

    def test_worker_refused_without_writes(self):
        for rid in self.run_ids:
            self._block_member(rid)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.family_retry("fakerepo", self.run_ids[0],
                                 "worker")
        self.assertEqual(ctx.exception.http_status, 403)
        fam_op = "prep-%s-%s" % (self.run_ids[0], self.batch_id)
        rec = read_json(batches.op_file(fam_op), default=None)
        self.assertEqual(rec["status"], "blocked")
        self.assertNotIn("superseded_by", rec)


class FamilyRetryRefusalTests(DriverFixture):
    def test_lone_member_has_no_train(self):
        rid = self._mk_run("left-pad", "2.0.0")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.family_retry("fakerepo", rid, "owner")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertIn("no release train", str(ctx.exception))

    def test_unreviewed_sibling_refused(self):
        ids = [self._mk_run(dep, "21.2.4") for dep in
               ("@commitlint/cli", "@commitlint/config-conventional",
                "commitlint")]
        with sched.locked("fakerepo"):
            qst = sched.load("fakerepo")
            for item in qst.get("queue") or []:
                if item.get("run_id") == ids[2]:
                    item["verified"] = False
            sched.save(qst)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.family_retry("fakerepo", ids[0], "owner")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertIn("리뷰 남음 1건", str(ctx.exception))


class FamilyExtrasTests(DriverFixture):
    def _mk_state(self, dep, target):
        run = store.create("fakerepo", "issue", "t", None, {"by": "t"})
        rid = run["id"]
        st = store.load(rid)
        st["dep"] = dep
        st["target"] = target
        st["lane"] = "next"
        store.save(st)
        return rid

    def _fam_rec(self, primary, members):
        return {"op": "prepare",
                "op_id": "prep-%s-base-1" % primary,
                "run_id": primary, "status": "done", "lane": "next",
                "batch_id": "base-1",
                "family": {"role": "primary", "members": members},
                "result": {"branch": "codex/f", "head": "d" * 40,
                           "fingerprint": "e" * 64}}

    def test_non_family_resumes_normally(self):
        extras, divert = m2._family_extras(
            "fakerepo", {"op": "prepare", "status": "done"})
        self.assertIsNone(extras)
        self.assertEqual(divert, "")

    def test_primary_reattaches_members(self):
        mb = self._mk_state("@commitlint/config-conventional",
                            "21.2.4")
        mc = self._mk_state("commitlint", "21.2.4")
        p = self._mk_state("@commitlint/cli", "21.2.4")
        rec = self._fam_rec(p, [p, mb, mc])
        extras, divert = m2._family_extras("fakerepo", rec)
        self.assertEqual(divert, "")
        self.assertEqual([e["run_id"] for e in extras], [mb, mc])
        self.assertEqual(extras[0]["dep"],
                         "@commitlint/config-conventional")

    def test_missing_member_waits(self):
        p = self._mk_state("@commitlint/cli", "21.2.4")
        rec = self._fam_rec(p, [p, "run-gone0000"])
        extras, divert = m2._family_extras("fakerepo", rec)
        self.assertIsNone(extras)
        self.assertEqual(divert, "wait")

    def test_batched_member_diverts_whole_resume(self):
        mb = self._mk_state("@commitlint/config-conventional",
                            "21.2.4")
        mc = self._mk_state("commitlint", "21.2.4")
        p = self._mk_state("@commitlint/cli", "21.2.4")
        write_json(batches.batch_file("hand-fam"),
                   {"id": "hand-fam", "repo": "fakerepo",
                    "updated_at": "2026-10-22T00:00:00+00:00",
                    "primaries": [{"dep": "commitlint",
                                   "target": "21.2.4",
                                   "lane": "next"}]})
        rec = self._fam_rec(p, [p, mb, mc])
        extras, divert = m2._family_extras("fakerepo", rec)
        self.assertIsNone(extras)
        self.assertEqual(divert, "dup:hand-fam")

    def test_resume_batches_whole_train(self):
        mb = self._mk_state("@commitlint/config-conventional",
                            "21.2.4")
        mc = self._mk_state("commitlint", "21.2.4")
        p = self._mk_run("@commitlint/cli", "21.2.4")
        rec = self._fam_rec(p, [p, mb, mc])
        self.op_recs[p] = rec
        from openmainty import fastcheck as _fc
        Path(_fc.op_fastcheck_path(rec["op_id"])).write_text(
            json.dumps({"ok": True, "steps": []}), encoding="utf-8")
        m2.set_policy("fakerepo", "batch", True)
        # FIX-a1: the batch stage enforces the enabled prefix, so
        # this whole-train resume runs with fastcheck on (the old
        # batch-with-fastcheck-off shape is now a refused skip).
        m2.set_policy("fakerepo", "fastcheck", True)
        seen = []
        real_stage = m2._run_batch_stage
        self.addCleanup(setattr, m2, "_run_batch_stage", real_stage)

        def fake_stage(repo, run_id, lane, stage_rec, base_id,
                       summary, _import=None, extra_primaries=None):
            seen.append(list(extra_primaries or []))
            return {"ran": True, "batch": "auto-x"}

        m2._run_batch_stage = fake_stage
        out = m2.tick_repo("fakerepo", _running="promoted",
                           _prepare=self._fake_prepare)
        self.assertEqual(len(seen), 1)
        self.assertEqual([e["run_id"] for e in seen[0]], [mb, mc])
        self.assertFalse(out.get("prepared"))
