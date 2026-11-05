"""Slice 285 (staged): one request per open for PR list and digest.

- A failing /api/prs plus 2 extra re-renders issues exactly 1 list
  request; pressing the retry button issues exactly 1 more.
- One #/digest visit (two concurrent renders) issues exactly 1
  /api/digest and 1 /api/digest?date=... request.
- A later quiet poll still re-fetches /api/digest: dedupe covers
  only concurrent in-flight requests, never the poll.
"""
import subprocess
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


def _line(src, prefix):
    for line in src.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError("missing line " + prefix)


def _dedupe_js(src):
    # Behavioral RED on the pre-fix tree: without the shipped helper,
    # concurrent renders each fetch, which is exactly the bug.
    if "function jgetDedupe(" not in src:
        return "function jgetDedupe(p) { return jget(p); }"
    return _line(src, "const inflightGets") + "\n" + _decl(src, "jgetDedupe")


def _run_node(prog):
    return subprocess.run(
        [NODE, "-e", prog], capture_output=True, text=True, timeout=60)


OK_STUB = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
async function tick() { await new Promise(r => setTimeout(r, 20)); }
'''

DIGEST_STUBS = OK_STUB + r'''
var view = {kind: "digest", date: ""};
var GETS = [];
async function jget(url) {
  GETS.push(url);
  if (url === "/api/digest") return {unknown: true, date: "2026-11-03"};
  if (url.indexOf("/api/digest?date=") === 0) return {unknown: true};
  throw new Error("unexpected GET " + url);
}
var box = {
  _html: "", _digestFp: undefined,
  set innerHTML(v) { this._html = String(v); },
  get innerHTML() { return this._html; },
  setAttribute: function() {}, removeAttribute: function() {},
  querySelector: function(sel) {
    if (sel === "[data-digestdate]") {
      if (this._html.indexOf("data-digestdate") === -1) return null;
      var m = /data-digestdate="([^"]*)"/.exec(this._html);
      return {dataset: {digestdate: m ? m[1] : ""}};
    }
    return null;
  },
  querySelectorAll: function() { return []; }
};
function $(id) { return (id === "view") ? box : {textContent: ""}; }
var CSS = {escape: function(s) { return String(s); }};
var M1_VERDICT_KO = {}, M1_VERDICT_CLS = {};
var SUB_APIKEY_RE = /x^/, SUB_LOGIN_RE = /x^/;
var APIKEY_SET_KO = "", LOGIN_NEEDED_KO = "";
var FETCH_ERR_TEXT = {};
function focusViewHeading() {}
function captureFk() { return null; }
function restoreFk() { return true; }
function focusViewH1() {}
'''

PR_STUBS = OK_STUB + r'''
var view = {kind: "repo", id: "r"};
var repoTab = "prs";
var GETS = [];
async function jget(url) {
  GETS.push(url);
  if (url.indexOf("/api/prs?repo=") === 0) throw new Error("boom");
  throw new Error("unexpected GET " + url);
}
var retryBtn = null;
var tabbox = {
  dataset: {}, _html: "",
  set innerHTML(v) { this._html = String(v); retryBtn = null; },
  get innerHTML() { return this._html; },
  querySelector: function(sel) {
    if (sel === "[data-k='pr-retry']") {
      if (this._html.indexOf("pr-retry") === -1) return null;
      if (!retryBtn) retryBtn = {onclick: null,
        click: function() { return this.onclick ? this.onclick() : null; }};
      return retryBtn;
    }
    return null;
  },
  querySelectorAll: function() { return []; }
};
function $(id) { return tabbox; }
function takeStashedReviewState() { return null; }
function captureReviewState() { return null; }
var SUB_APIKEY_RE = /x^/, SUB_LOGIN_RE = /x^/;
var APIKEY_SET_KO = "", LOGIN_NEEDED_KO = "";
var FETCH_ERR_TEXT = {};
'''


def _digest_prog(src, extra):
    decls = ["esc", "subErrKo", "fetchErrKo", "errBlockHtml",
             "errNewToBox", "digestPrevDay", "digestFp",
             "digestPaintedFor", "digestReqFresh", "digestReqClear",
             "fmtKST", "fmtKSThm", "digestHeadHtml", "digestNeedHtml",
             "digestRepoHtml", "digestEmptyHtml",
             "renderDigestView"]
    needko = next(line for line in src.splitlines()
                  if line.startswith("const DG_NEED_KO"))
    return (DIGEST_STUBS + _dedupe_js(src) + "\n" + needko + "\n"
            + "\n".join(_decl(src, n) for n in decls)
            + "\n" + extra)


def _pr_prog(src, extra):
    decls = ["esc", "subErrKo", "fetchErrKo", "errBlockHtml",
             "paintRepoPrs"]
    return (PR_STUBS + _dedupe_js(src) + "\n"
            + _line(src, "let prCache = ") + "\n"
            + "\n".join(_decl(src, n) for n in decls)
            + "\n" + extra)


class PrListDedupeCase(unittest.TestCase):
    def test_failed_list_rerenders_reuse_one_request(self):
        prog = _pr_prog(_script(), r'''
(async () => {
  await paintRepoPrs({name: "r"});
  await paintRepoPrs({name: "r"});
  await paintRepoPrs({name: "r"});
  var n1 = GETS.filter(u => u.indexOf("/api/prs?repo=") === 0).length;
  ok(n1 === 1, "one-list-get:" + n1);
  ok(tabbox.innerHTML.indexOf("다시 시도") !== -1, "retry-shown");
  var btn = tabbox.querySelector("[data-k='pr-retry']");
  ok(!!btn, "retry-present");
  await btn.click();
  await tick();
  var n2 = GETS.filter(u => u.indexOf("/api/prs?repo=") === 0).length;
  ok(n2 === 2, "retry-one-more:" + n2);
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)


class DigestDedupeCase(unittest.TestCase):
    def test_one_visit_fetches_once_each(self):
        prog = _digest_prog(_script(), r'''
(async () => {
  await Promise.all([renderDigestView(""), renderDigestView("")]);
  await tick();
  var base = GETS.filter(u => u === "/api/digest").length;
  var prev = GETS.filter(u => u.indexOf("/api/digest?date=") === 0).length;
  ok(base === 1, "one-base:" + base);
  ok(prev === 1, "one-prev:" + prev);
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_boot_second_render_skips_while_fresh(self):
        # Models render()'s digest branch: right after a paint the
        # follow-up render skips the fetch; after aging it fetches.
        prog = _digest_prog(_script(), r'''
(async () => {
  await renderDigestView("");
  await tick();
  ok(digestReqFresh(box, "") === true, "fresh-skips");
  ok(digestReqFresh(box, "2026-01-01") === false, "date-mismatch-fetches");
  box._digestReqAt -= 30000;
  ok(digestReqFresh(box, "") === false, "aged-fetches");
  var n1 = GETS.filter(u => u === "/api/digest").length;
  await renderDigestView("");
  await tick();
  var n2 = GETS.filter(u => u === "/api/digest").length;
  ok(n1 === 1 && n2 === 2, "aged-refetch:" + n1 + "/" + n2);
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_back_nav_within_window_renders_digest(self):
        # L30: digest -> glance -> digest within 5 s. render() clears
        # the request marker for non-digest views, so coming back
        # re-renders the digest instead of keeping the glance.
        prog = _digest_prog(_script(), r'''
(async () => {
  await renderDigestView("");
  await tick();
  ok(digestReqFresh(box, "") === true, "fresh-after-paint");
  box.innerHTML = "<h1>한눈에 보기</h1><div>오늘 결정할 일</div>";
  digestReqClear(box);
  ok(digestReqFresh(box, "") === false, "stale-after-leave");
  var n1 = GETS.filter(u => u === "/api/digest").length;
  await renderDigestView("");
  await tick();
  var n2 = GETS.filter(u => u === "/api/digest").length;
  ok(n2 === n1 + 1, "back-nav-refetch:" + n1 + "/" + n2);
  ok(box.innerHTML.indexOf("오늘 결정할 일") === -1, "glance-gone");
  ok(box.innerHTML.indexOf("data-digestdate") !== -1, "digest-painted");
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_quiet_poll_still_refetches(self):
        prog = _digest_prog(_script(), r'''
(async () => {
  await renderDigestView("");
  await tick();
  var n1 = GETS.filter(u => u === "/api/digest").length;
  await renderDigestView("");
  await tick();
  var n2 = GETS.filter(u => u === "/api/digest").length;
  ok(n1 === 1 && n2 === 2, "poll-refetch:" + n1 + "/" + n2);
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)


class RenderBranchPinCase(unittest.TestCase):
    def test_render_digest_branch_uses_fresh_guard(self):
        src = _script()
        self.assertIn(
            'if (!digestReqFresh($("view"), view.date || ""))'
            ' renderDigestView(view.date || "");',
            src)

    def test_render_clears_digest_marker_off_digest(self):
        src = _script()
        self.assertIn(
            'if (view.kind !== "digest") digestReqClear($("view"));',
            src)


if __name__ == "__main__":
    unittest.main()
