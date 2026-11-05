"""Slice 278b follow-ups: L19 typed no-checkout watch, L20 metrics None.

Failing-first: L19 api_watch must answer a typed 200 (not 400) when the
repo never linked a checkout, and the repo work tab must show an honest
neutral state; L20 render_text must never print a raw None.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import metrics, repos, serve  # noqa: E402

try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:
    from _home import use_temp_home  # noqa: E402


def _script():
    base = Path(__file__).resolve().parent.parent
    return (base / "src" / "openmainty" / "dashboard.html").read_text(
        encoding="utf-8")


def _decl(src, name):
    """Brace-balanced body of `function name` / `async function name`."""
    for prefix in ("async function " + name + "(",
                   "function " + name + "("):
        i = src.find(prefix)
        if i != -1:
            break
    else:
        raise AssertionError("missing function " + name)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("unbalanced " + name)


class L19ServeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        repos.save({"name": "nolinkrepo", "github": "example/nolinkrepo",
                    "baseBranch": "main", "knowledge": [], "checks": [],
                    "e2e": []})

    def test_no_checkout_is_typed_non_error(self):
        body = serve.api_watch({"repo": "nolinkrepo"})
        self.assertTrue(body.get("no_checkout"))
        for key in ("drift", "errors", "checked", "warnings",
                    "warning_notes", "registry_errors", "unsupported",
                    "issues"):
            self.assertIn(key, body)
        self.assertEqual(body["drift"], [])
        self.assertEqual(body["issues"], [])

    def test_real_watch_errors_stay_errors(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_watch({"repo": "nosuchrepo"})
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_watch({"repo": ""})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_watch({})
        self.assertEqual(ctx.exception.status, 400)

    def test_linked_but_missing_checkout_stays_400(self):
        from openmainty import pcconfig
        pc = pcconfig.load()
        pc["checkouts"]["nolinkrepo"] = str(
            Path(self.tmp.name) / "gone-dir")
        pcconfig.save(pc)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_watch({"repo": "nolinkrepo"})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("checkout not found", ctx.exception.message)


class L19ClientCase(unittest.TestCase):
    def test_diag_repo_maps_no_checkout(self):
        body = _decl(_script(), "diagRepo")
        self.assertIn("no_checkout", body)
        self.assertIn("noCheckout", body)

    def test_work_tab_shows_neutral_unlinked_state(self):
        body = _decl(_script(), "paintRepoWork")
        self.assertIn("noCheckout", body)
        self.assertIn("체크아웃 미연결", body)
        # Neutral note, never the error style.
        i = body.index("if (c.noCheckout)")
        branch = body[i:body.index("return; }", i)]
        self.assertIn("repo-empty", branch)
        self.assertNotIn("inline-err", branch)


class L20MetricsCase(unittest.TestCase):
    def _data(self):
        return {"generated_at": "2026-11-03T00:00:00Z",
                "window_days": 7, "repos": {},
                "pushes_by_openmainty": 0, "push_basis": "test basis"}

    def _row(self, since=None, held=()):
        return {"autonomy_since": since,
                "prepares": {"done": 0, "re_resolve": 0, "rederive": 0,
                             "blocked": 0, "stale": 0},
                "fastcheck": {"green": 0, "red": 0, "direct": 0,
                              "via-tool": 0, "not-run": 0, "ci-only": 0,
                              "unknown": 0, "archived_attempts": 0},
                "auto_batches": {"total": 0, "held": list(held),
                                 "waiting_on_root_batches": 0,
                                 "waiting_on_root_gates": 0},
                "duplicate_skips": 0}

    def test_missing_start_is_honest_both_languages(self):
        data = self._data()
        data["repos"] = {"r": self._row(since=None)}
        ko = metrics.render_text(data)
        self.assertIn("자율 시작 기록 없음", ko)
        self.assertNotIn("None", ko)
        en = metrics.render_text(data, lang="en")
        self.assertIn("autonomy since none", en)
        self.assertNotIn("None", en)

    def test_real_start_still_renders(self):
        data = self._data()
        data["repos"] = {"r": self._row(since="2026-10-20T10:00:00+00:00")}
        self.assertIn("자율 시작 2026-10-20",
                      metrics.render_text(data))
        self.assertIn("autonomy since 2026-10-20",
                      metrics.render_text(data, lang="en"))

    def test_empty_hold_fields_are_honest(self):
        data = self._data()
        data["repos"] = {"r": self._row(
            since="2026-10-20T10:00:00+00:00",
            held=[{"id": "b1", "by": "", "reason": ""}])}
        ko = metrics.render_text(data)
        self.assertIn("보류: b1 (기록 없음) — 기록 없음", ko)
        en = metrics.render_text(data, lang="en")
        self.assertIn("held: b1 by none — none", en)
