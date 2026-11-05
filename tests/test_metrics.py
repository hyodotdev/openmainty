"""M3.4a: `openmainty metrics` regenerates every number.

Fixture-home tests (counts per census status, held batch with a
reason, duplicate skips, --json shape, --lang en), plus the
no-push-code-path grep test over src/ and a docs smoke test.
Py3.9 portable.
"""
import ast
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import cli as _cli  # noqa: E402
from openmainty import metrics  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def _j(obj):
    return json.dumps(obj, ensure_ascii=False)


class MetricsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        for sub in ("repos", "integration_ops", "batches",
                    "runs/run-1", "runs/run-2", "runs/run-3"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "repos" / "fakerepo.json").write_text(
            _j({"name": "fakerepo"}), encoding="utf-8")
        (self.home / "events.jsonl").write_text(
            "\n".join([
                _j({"at": "2026-10-20T10:00:00+00:00",
                    "repo": "fakerepo", "kind": "m2-policy",
                    "title": "자율 준비 켜짐"}),
                _j({"at": "2026-10-21T10:00:00+00:00",
                    "repo": "fakerepo", "kind": "m2-skip",
                    "title": "자율 묶음 건너뜀 12건: 이미 묶음 b에 포함"}),
                _j({"at": "2026-10-21T11:00:00+00:00",
                    "repo": "fakerepo", "kind": "m2-skip",
                    "title": "자율 묶음 건너뜀 16건: 이미 묶음 b에 포함"}),
                _j({"at": "2026-10-21T12:00:00+00:00",
                    "repo": "fakerepo", "kind": "m2-skip",
                    "title": "자율 묶음 건너뜀 17건: 이미 묶음 b에 포함"}),
                _j({"at": "2026-10-21T13:00:00+00:00",
                    "repo": "fakerepo", "kind": "m2-done",
                    "ref": "done-run-1",
                    "title": "자율 준비 완료"}),
                _j({"at": "2026-10-21T14:00:00+00:00",
                    "repo": "fakerepo", "kind": "m2-done",
                    "ref": "done-run-3",
                    "title": "자율 준비 완료",
                    "detail": "3단계 전부 통과 · 파생 잠금 출처"}),
                "not json at all",
            ]) + "\n", encoding="utf-8")
        for run_id in ("run-1", "run-2", "run-3"):
            (self.home / f"runs/{run_id}/run.json").write_text(
                _j({"id": run_id, "repo": "fakerepo"}), encoding="utf-8")
        ops = self.home / "integration_ops"
        (ops / "op-prep1.json").write_text(_j(
            {"op": "prepare", "origin": "m2", "repo": "fakerepo",
             "status": "done", "run_id": "run-1"}), encoding="utf-8")
        (ops / "op-prep1.fastcheck.json").write_text(_j(
            {"ok": True,
             "exercised": {"status": "direct"}}), encoding="utf-8")
        (ops / "op-prep1.fastcheck.attempt-1.json").write_text(
            _j({"ok": False}), encoding="utf-8")
        (ops / "op-prep2.json").write_text(_j(
            {"op": "prepare", "origin": "m2", "repo": "fakerepo",
             "status": "blocked", "run_id": "run-2"}), encoding="utf-8")
        (ops / "op-prep2.fastcheck.json").write_text(_j(
            {"ok": False,
             "exercised": {"status": "via-tool",
                           "via": {"gate": "lint"}}}), encoding="utf-8")
        (ops / "op-ui.json").write_text(_j(
            {"op": "prepare", "origin": "ui", "repo": "fakerepo",
             "status": "done", "run_id": "run-1"}), encoding="utf-8")
        # A blocked prepare rescued by a derived lockfix done: the
        # run counts done (via re-resolve), never blocked.
        (ops / "op-prep3.json").write_text(_j(
            {"op": "prepare", "origin": "m2", "repo": "fakerepo",
             "status": "blocked", "run_id": "run-3"}), encoding="utf-8")
        (ops / "op-prep3-lockfix.json").write_text(_j(
            {"op": "reresolve", "origin": "m2", "repo": "fakerepo",
             "status": "done", "run_id": "run-3",
             "derived_from": "op-prep3"}), encoding="utf-8")
        batches = self.home / "batches"
        (batches / "index.json").write_text(_j({"batches": []}),
                                            encoding="utf-8")
        (batches / "auto-1.json").write_text(_j(
            {"id": "auto-1", "repo": "fakerepo",
             "origin_m2": "op-prep1",
             "hold": {"by": "root",
                      "reason": "RN lockstep: metro 0.88 needs RN 0.88",
                      "at": "2026-10-21T11:00:00+00:00"},
             "integrations": [{"gates": [
                 {"key": "worker:unit", "status": "pass"},
                 {"key": "unit", "status": "pending"},
                 {"key": "lint", "status": "pending"}]}]}),
            encoding="utf-8")
        (batches / "manual-1.json").write_text(_j(
            {"id": "manual-1", "repo": "fakerepo",
             "integrations": []}), encoding="utf-8")

    def test_collect_counts(self):
        data = metrics.collect(home=self.home)
        row = data["repos"]["fakerepo"]
        self.assertEqual(row["autonomy_since"],
                         "2026-10-20T10:00:00+00:00")
        # Done comes from the done-run events (run-1 direct,
        # run-3 via lock re-resolve). run-3's blocked prepare op is
        # not a block — its done event wins. UI-origin ops never
        # count. run-2's blocked prepare with no done event is the
        # one block.
        self.assertEqual(row["prepares"],
                         {"done": 2, "re_resolve": 1, "blocked": 1,
                          "stale": 0, "rederive": 0})
        fc = row["fastcheck"]
        self.assertEqual((fc["green"], fc["red"]), (1, 1))
        self.assertEqual(
            (fc["direct"], fc["via-tool"], fc["not-run"],
             fc["ci-only"], fc["unknown"], fc["archived_attempts"]),
            (1, 1, 0, 0, 0, 1))
        auto = row["auto_batches"]
        self.assertEqual(auto["total"], 1)
        self.assertEqual(len(auto["held"]), 1)
        self.assertEqual(auto["held"][0]["by"], "root")
        self.assertIn("lockstep", auto["held"][0]["reason"])
        self.assertEqual(len(auto["lockstep_or_native_holds"]), 1)
        self.assertEqual(auto["waiting_on_root_batches"], 1)
        self.assertEqual(auto["waiting_on_root_gates"], 2)
        # Repeated notes report the CURRENT number, not a sum.
        self.assertEqual(row["duplicate_skips"], 17)
        self.assertEqual(data["pushes_by_openmainty"], 0)

    def test_manual_lockfix_recovery_counts_done_once(self):
        # FIX-a3 (Codex F15): the live jest 30.5.2 shape — a blocked
        # m2 prepare recovered through a MANUAL (ui-origin) lockfix
        # done plus a resume done, with NO m2-done event (the manual
        # path emits none). One current outcome per run: done
        # (re_resolve +1), never blocked.
        (self.home / "runs/run-4").mkdir(parents=True)
        (self.home / "runs/run-4/run.json").write_text(
            _j({"id": "run-4", "repo": "fakerepo"}), encoding="utf-8")
        ops = self.home / "integration_ops"
        (ops / "op-prep4.json").write_text(_j(
            {"op": "prepare", "origin": "m2", "repo": "fakerepo",
             "status": "blocked", "run_id": "run-4"}), encoding="utf-8")
        (ops / "op-prep4-lockfix.json").write_text(_j(
            {"op": "reresolve", "origin": "ui", "repo": "fakerepo",
             "status": "done", "run_id": "run-4",
             "derived_from": "op-prep4"}), encoding="utf-8")
        data = metrics.collect(home=self.home)
        self.assertEqual(data["repos"]["fakerepo"]["prepares"],
                         {"done": 3, "re_resolve": 2, "blocked": 1,
                          "stale": 0, "rederive": 0})

    def test_fully_manual_run_is_excluded_entirely(self):
        # M27FIXA-F1: a MANUAL prepare (origin None, pre-autonomy)
        # plus a manual reresolve done is not autonomy work — not
        # done, not blocked, not stale. Live shape: run-18aa3c03,
        # run-3d56a67e, run-9f8bcebb (M2 only fastchecked them).
        (self.home / "runs/run-6").mkdir(parents=True)
        (self.home / "runs/run-6/run.json").write_text(
            _j({"id": "run-6", "repo": "fakerepo"}), encoding="utf-8")
        ops = self.home / "integration_ops"
        (ops / "op-prep6.json").write_text(_j(
            {"op": "prepare", "repo": "fakerepo",
             "status": "blocked", "run_id": "run-6"}), encoding="utf-8")
        (ops / "op-prep6-lockfix.json").write_text(_j(
            {"op": "reresolve", "repo": "fakerepo",
             "status": "done", "run_id": "run-6",
             "derived_from": "op-prep6"}), encoding="utf-8")
        data = metrics.collect(home=self.home)
        self.assertEqual(data["repos"]["fakerepo"]["prepares"],
                         {"done": 2, "re_resolve": 1, "blocked": 1,
                          "stale": 0, "rederive": 0})

    def test_ci_consumers_split_out_of_not_run(self):
        # M2.8a1: a not-run receipt naming CI consumers counts
        # ci-only; the not-run count drops by the same number.
        ops = self.home / "integration_ops"
        (ops / "op-ci1.json").write_text(_j(
            {"op": "prepare", "origin": "m2", "repo": "fakerepo",
             "status": "done", "run_id": "run-1"}), encoding="utf-8")
        (ops / "op-ci1.fastcheck.json").write_text(_j(
            {"ok": True,
             "exercised": {"status": "not-run",
                           "ci_consumers": ["ci-web.yml"]}}),
            encoding="utf-8")
        data = metrics.collect(home=self.home)
        fc = data["repos"]["fakerepo"]["fastcheck"]
        self.assertEqual(
            (fc["direct"], fc["via-tool"], fc["not-run"],
             fc["ci-only"], fc["unknown"], fc["archived_attempts"]),
            (1, 1, 0, 1, 0, 1))

    def test_stale_marked_record_counts_stale_not_blocked(self):
        # FIX-a4: a blocked prepare carrying the stale marker
        # (base moved the dep's own line) counts as stale
        # refresh-stage work, never as blocked.
        (self.home / "runs/run-5").mkdir(parents=True)
        (self.home / "runs/run-5/run.json").write_text(
            _j({"id": "run-5", "repo": "fakerepo"}), encoding="utf-8")
        ops = self.home / "integration_ops"
        (ops / "op-prep5.json").write_text(_j(
            {"op": "prepare", "origin": "m2", "repo": "fakerepo",
             "status": "blocked", "run_id": "run-5",
             "error": "기준이 이미 bob를 0.43.2로 올림 — 0.44.1 후보를 "
                      "현재 기준에서 다시 만들어야 함",
             "stale": {"dep": "bob", "target": "0.44.1",
                       "base_version": "0.43.2"}}), encoding="utf-8")
        data = metrics.collect(home=self.home)
        self.assertEqual(data["repos"]["fakerepo"]["prepares"],
                         {"done": 2, "re_resolve": 1, "blocked": 1,
                          "stale": 1, "rederive": 0})

    def test_json_shape(self):
        data = metrics.collect(home=self.home)
        text = json.dumps(data, ensure_ascii=False)
        back = json.loads(text)
        self.assertIn("generated_at", back)
        self.assertIsNone(back["window_days"])
        self.assertIn("fakerepo", back["repos"])
        self.assertIn("push_basis", back)

    def test_lang_en(self):
        data = metrics.collect(home=self.home)
        out = metrics.render_text(data, lang="en")
        self.assertIn("autonomy since 2026-10-20", out)
        self.assertIn("pushes by OpenMainty: 0", out)
        ko = metrics.render_text(data)
        self.assertIn("자율 시작 2026-10-20", ko)
        self.assertIn("푸시한 횟수: 0회", ko)

    def test_days_window(self):
        # Backdate the flow files 12 days; the window then drops
        # them while batch hold state stays current, labelled so.
        import time as _time
        old = _time.time() - 12 * 86400
        ops = self.home / "integration_ops"
        for name in ("op-prep1.json", "op-prep2.json",
                     "op-prep3.json", "op-prep3-lockfix.json",
                     "op-prep1.fastcheck.json",
                     "op-prep2.fastcheck.json",
                     "op-prep1.fastcheck.attempt-1.json"):
            os.utime(ops / name, (old, old))
        data = metrics.collect(home=self.home, days=1)
        row = data["repos"]["fakerepo"]
        self.assertEqual(row["prepares"],
                         {"done": 0, "re_resolve": 0, "blocked": 0,
                          "stale": 0, "rederive": 0})
        self.assertEqual(row["fastcheck"]["green"], 0)
        self.assertEqual(
            row["fastcheck"]["archived_attempts"], 0)
        self.assertEqual(len(row["auto_batches"]["held"]), 1)

    def test_cli_metrics(self):
        # OPENMAINTY_HOME already points at the fixture home.
        args = _cli.build_parser().parse_args(
            ["metrics", "--lang", "en"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _cli.cmd_metrics(args)
        self.assertEqual(rc, 0)
        self.assertIn("pushes by OpenMainty: 0", buf.getvalue())
        bad = _cli.build_parser().parse_args(
            ["metrics", "--lang", "fr"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_cli.cmd_metrics(bad), 2)
        bad_days = _cli.build_parser().parse_args(
            ["metrics", "--days", "0"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_cli.cmd_metrics(bad_days), 2)


class RederiveCountsTests(MetricsFixture):
    def test_rederive_counted_next_to_re_resolve(self):
        # run-4: an m2 prepare blocked, rescued by a derived
        # re-apply done, with the re-apply sentence in its done
        # event. It counts done + re_resolve + rederive, never
        # blocked.
        home = self.home
        ops = home / "integration_ops"
        (ops / "op-prep4.json").write_text(_j(
            {"op": "prepare", "origin": "m2", "repo": "fakerepo",
             "status": "blocked", "run_id": "run-4"}), encoding="utf-8")
        (ops / "op-prep4-rederive.json").write_text(_j(
            {"op": "reresolve", "kind": "rederive", "origin": "m2",
             "repo": "fakerepo", "status": "done", "run_id": "run-4",
             "derived_from": "op-prep4"}), encoding="utf-8")
        (home / "runs/run-4/run.json").parent.mkdir(
            parents=True, exist_ok=True)
        (home / "runs/run-4/run.json").write_text(
            _j({"id": "run-4", "repo": "fakerepo"}), encoding="utf-8")
        evf = home / "events.jsonl"
        with open(evf, "a", encoding="utf-8") as fh:
            fh.write(_j({"at": "2026-10-21T15:00:00+00:00",
                         "repo": "fakerepo", "kind": "m2-done",
                         "ref": "done-run-4",
                         "title": "자율 준비 완료 foo 1.0.0 -> 1.2.0",
                         "detail": "기준 next · 기준에서 다시 적용 — "
                                   "원본 커밋과 같은 버전 변경"}) + "\n")
        data = metrics.collect(home=home)
        self.assertEqual(data["repos"]["fakerepo"]["prepares"],
                         {"done": 3, "re_resolve": 2, "blocked": 1,
                          "stale": 0, "rederive": 1})


class NoPushCodePathTests(unittest.TestCase):
    """Every 'git push' / 'gh pr create|edit' literal in src/ lives
    in a draft-builder function (copy-only owner text); no _run_git
    argv literal carries a push verb. If either trips, the metrics
    'pushes: 0' claim needs re-examination — do not weaken this."""

    def test_push_literals_are_draft_text_only(self):
        root = Path(__file__).resolve().parent.parent / "src" \
            / "openmainty"
        offenders = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and \
                            isinstance(sub.value, str) and (
                                "git push" in sub.value
                                or "gh pr create" in sub.value
                                or "gh pr edit" in sub.value):
                        if "draft" not in node.name:
                            offenders.append(
                                f"{path.name}:{node.name}")
        self.assertEqual(offenders, [])

    def test_no_push_verb_in_git_argv(self):
        root = Path(__file__).resolve().parent.parent / "src" \
            / "openmainty"
        verbs = set()
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                is_rg = (isinstance(func, ast.Name)
                         and func.id == "_run_git") or \
                    (isinstance(func, ast.Attribute)
                     and func.attr == "_run_git")
                if not is_rg:
                    continue
                first = node.args[0]
                if isinstance(first, ast.List):
                    for elt in first.elts:
                        if isinstance(elt, ast.Constant) and \
                                isinstance(elt.value, str):
                            verbs.add(elt.value)
        self.assertNotIn("push", verbs)


class HackathonDocsTests(unittest.TestCase):
    def test_hackathon_doc_links_runbook(self):
        base = Path(__file__).resolve().parent.parent
        text = (base / "docs" / "HACKATHON.md").read_text(
            encoding="utf-8")
        self.assertIn("openmainty metrics", text)
        self.assertIn("OWNER_RUNBOOK.md", text)
        self.assertIn("never pushes", text)
        # The basis split stays in the file.
        self.assertIn("Basis vs new work", text)
        # M3.4b factual fixes: no phantom subcommand, no
        # web-build step claim.
        self.assertNotIn("openmainty rollback", text)
        self.assertNotIn("web-build", text)


if __name__ == "__main__":
    unittest.main()
