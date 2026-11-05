"""Slice 279 follow-ups: L21 boot diag, L22 driftKo mapper.

L21: a cold boot on a repo hash must start diagRepo exactly once (as
openRepo does), with no duplicate watch POST from the first paint or
the poll repaint. L22: every known server drift shape renders Korean,
unknown shapes fall back to a Korean lead plus the raw string.
"""
import subprocess
import sys
import unittest
from pathlib import Path

NODE = "node"


def _script():
    base = Path(__file__).resolve().parent.parent
    return (base / "src" / "openmainty" / "dashboard.html").read_text(
        encoding="utf-8")


def _decl(src, name):
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


def _run_node(prog):
    return subprocess.run(
        [NODE, "-e", prog], capture_output=True, text=True, timeout=60)


L21_STUBS = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
// Fake DOM: only the properties renderRepo touches.
function fakeEl() { return {}; }
var __els = {};
function $(id) {
  if (!__els[id]) __els[id] = fakeEl();
  return __els[id];
}
var O = {repos: [{name: "demo", github: "", checkout: "",
                  branch_next_reason: "", has_next: false,
                  drift: "checkout not linked"}]};
var view = {kind: "repo", id: "demo"};
var repoTab = "work";
var checks = {};
var diagFp = {};
var pendingRevState = null;
var posts = [];
var renders = 0;
async function jpost(url, body) { posts.push(url); return {no_checkout: true}; }
function render() { renders++; renderRepo(); }
function repoTabsHtml() { return ""; }
function repoTabPanelHtml() { return ""; }
function focusViewHeading() {}
function wireRepoTabs() {}
function nextLabel() { return ""; }
function nextProv() { return ""; }
function stashReviewState() {}
function runsFp() { return "fp"; }
function paintRepoOverview() {}
function paintRepoReviews() {}
function paintRepoPrs() {}
function paintRepoCommunity() {}
function paintRepoWork() {}
function paintRepoDeps() {}
function paintRepoBranches() {}
function paintRepoActivity() {}
function startPlan() {}
async function tick() { await new Promise(r => setTimeout(r, 50)); }
'''


class L21BootDiagCase(unittest.TestCase):
    def _prog(self, extra):
        src = _script()
        return (L21_STUBS + _decl(src, "esc") + "\n"
                + _decl(src, "driftKo") + "\n"
                + _decl(src, "captureFk") + "\n"
                + _decl(src, "restoreFk") + "\n"
                + _decl(src, "focusViewH1") + "\n"
                + _decl(src, "repoWmenuOpen") + "\n"
                + _decl(src, "setRepoWmenuOpen") + "\n"
                + _decl(src, "restoreDiagFocus") + "\n"
                + _decl(src, "captureRepoTabFocus") + "\n"
                + _decl(src, "restoreRepoTabFocus") + "\n"
                + _decl(src, "diagRepo") + "\n"
                + _decl(src, "openRepo") + "\n"
                + _decl(src, "renderRepo") + "\n"
                + extra)

    def test_cold_boot_starts_diag_exactly_once(self):
        prog = self._prog(r'''
(async () => {
  renderRepo();  // what boot's loadAll->render does on a repo hash
  await tick(); await tick();
  ok(posts.length === 1, "boot-posts-once:" + posts.length);
  ok(checks.demo && checks.demo.noCheckout === true, "boot-state-set");
  ok(renders < 10, "boot-no-render-loop:" + renders);
  // The 30 s poll repaint must not re-diagnose.
  renderRepo();
  await tick();
  ok(posts.length === 1, "poll-no-repost:" + posts.length);
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_in_app_nav_still_posts_once(self):
        prog = self._prog(r'''
(async () => {
  openRepo("demo");  // sidebar click path
  await tick(); await tick();
  ok(posts.length === 1, "nav-posts-once:" + posts.length);
  ok(checks.demo && checks.demo.noCheckout === true, "nav-state-set");
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)


L22_CASES = [
    ("checkout not linked", "미연결"),
    ("no dependencies declared", "의존성 없음"),
    ("DRIFT: a, b", "드리프트: a, b"),
    ("unpublished: x", "미배포: x"),
    ("errors: 2", "오류 2건"),
    ("ok (3 tracked)", "정상"),
    ("check failed: boom", "점검 실패"),
    ("manifest 5개 추적 중 (최근 스캔 없음; 정확한 drift는 스캔 실행 후 표시)",
     "스캔 전 · 목록만 추적 중"),
    ("DRIFT: a; unpublished: b", "드리프트: a; 미배포: b"),
    ("bogus future shape", "미확인 상태: bogus future shape"),
    ("", "기록 없음"),
]


class L22DriftKoCase(unittest.TestCase):
    def test_every_shape_renders_korean(self):
        src = _script()
        lines = [L21_STUBS.split("var O = ")[0],
                 _decl(src, "esc"),
                 _decl(src, "driftKo")]
        for raw, want in L22_CASES:
            lines.append(
                'ok(driftKo(%r) === %r, "driftko:%s");'
                % (raw, want, raw[:24]))
        lines.append('console.log("PASS-COUNT " + pass);')
        proc = _run_node("\n".join(lines))
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_mapper_keeps_payload_callers_escape(self):
        # driftKo never strips: names (even hostile ones) survive, and
        # every display site passes the result through esc().
        src = _script()
        prog = (L21_STUBS.split("var O = ")[0]
                + _decl(src, "esc") + "\n" + _decl(src, "driftKo") + "\n"
                + r'''
var evil = "<img src=x onerror=alert(1)>";
var out = driftKo("DRIFT: " + evil);
ok(out.indexOf(evil) !== -1, "payload-kept");
ok(esc(out).indexOf("<img") === -1, "esc-covers");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        body = _decl(src, "renderRepo")
        self.assertIn("esc(driftKo(r.drift))", body)
        self.assertIn('title="${esc(r.drift || "")}"', body)
