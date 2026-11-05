"""Slice 280: focus survives re-renders (L23, L24a, L24b) + digest alert (L15b).

L23: diagRepo's own re-render keeps details.wmenu open and returns
focus to the starter (#diagbtn / #tab-retry via data-fk, else h1),
but never moves focus after a navigation meanwhile.
L24a: the quiet poll restores repo header focus by data-fk key.
L24b: a quiet digest repaint with an unchanged fingerprint leaves
the DOM alone; a changed one repaints and restores focus by key.
L15b: the digest error alerts once and offers a working retry.
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

L23_STUBS = r'''
var checks = {};
var view = {kind: "repo", id: "demo"};
var diagFp = {};
var pendingRevState = null;
var posts = [];
var renders = 0;
var focusLog = [];
function mkHeadBtn(fk) {
  var b = {dataset: {fk: fk},
           focus: function() {
             focusLog.push(fk);
             document.activeElement = b;
           }};
  return b;
}
var wmenu = {open: true};
var diagBtn = mkHeadBtn("repohead-diag");
var summaryEl = mkHeadBtn("repohead-info");
function mkH1() {
  var h = {tagName: "H1",
           focus: function() {
             focusLog.push("h1");
             document.activeElement = h;
           }};
  return h;
}
var h1El = mkH1();
var tabStrip = null;
var viewEl = {
  contains: function() { return true; },
  querySelector: function(sel) {
    if (sel === ".tabs") return tabStrip;
    var m = /data-fk="([^"]+)"/.exec(sel || "");
    if (m) {
      if (m[1] === "repohead-diag") return diagBtn;
      if (m[1] === "repohead-info") return summaryEl;
      return null;
    }
    if (sel === "h1") return h1El;
    if (sel === "h2") return null;
    return null;
  }
};
var document = {
  activeElement: diagBtn,
  querySelector: function(sel) {
    return (sel === "details.wmenu") ? wmenu : null;
  }
};
function $(id) { return (id === "view") ? viewEl : null; }
var CSS = {escape: function(s) { return String(s); }};
function stashReviewState() {}
function render() {
  renders++;
  wmenu = {open: false};
  diagBtn = mkHeadBtn("repohead-diag");
  summaryEl = mkHeadBtn("repohead-info");
  h1El = mkH1();
  tabStrip = mkTabStrip();
  document.activeElement = {tagName: "BODY"};
}
function runsFp() { return "fp"; }
var postGate = null;
async function jpost(url, body) {
  posts.push(url);
  if (typeof __mutateView === "function") __mutateView();
  if (postGate) await postGate;
  return {no_checkout: true};
}
var REPO_TABS = [["overview"], ["reviews"], ["prs"], ["community"],
                 ["work"], ["deps"], ["branches"], ["activity"]];
function mkTabStrip() {
  var btn = {dataset: {tab: "reviews"},
             focus: function() {
               focusLog.push("tab:reviews");
               document.activeElement = btn;
             },
             tabIndex: 0};
  return {
    scrollLeft: 0, scrollWidth: 0, clientWidth: 0,
    _btn: btn,
    querySelectorAll: function() { return [this._btn]; },
    querySelector: function(sel) {
      return (sel === "#repotab-reviews") ? this._btn : null;
    }
  };
}
'''


class L23DiagFocusCase(unittest.TestCase):
    def _prog(self, extra):
        src = _script()
        decls = ["captureFk", "restoreFk", "focusViewH1",
                 "repoWmenuOpen", "setRepoWmenuOpen",
                 "restoreDiagFocus",
                 "captureRepoTabFocus", "restoreRepoTabFocus", "showRepoTab",
                 "diagRepo"]
        return (OK_STUB + L23_STUBS
                + "\n".join(_decl(src, n) for n in decls)
                + "\n" + extra)

    def test_diag_rerender_keeps_details_and_focus(self):
        prog = self._prog(r'''
(async () => {
  await diagRepo("demo");
  await tick();
  ok(posts.length === 1, "diag-posts-once:" + posts.length);
  ok(renders === 2, "diag-renders-twice:" + renders);
  ok(wmenu.open === true, "diag-details-open");
  ok(focusLog[focusLog.length - 1] === "repohead-diag",
     "diag-focus-diagbtn:" + JSON.stringify(focusLog));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_diag_rerender_after_nav_moves_no_focus(self):
        prog = self._prog(r'''
(async () => {
  var release;
  postGate = new Promise(r => { release = r; });
  var done = diagRepo("demo");
  await tick();
  // Navigate while the POST is pending: the completion must
  // neither refocus nor touch details. (The loading restore ran
  // before the navigation, while the view still matched.)
  view = {kind: "command"};
  var n0 = focusLog.length;
  var wmenuNow = wmenu;
  var openNow = wmenu.open;
  release();
  await done;
  await tick();
  ok(posts.length === 1, "nav-posts-once:" + posts.length);
  ok(focusLog.length === n0, "nav-no-focus:" + JSON.stringify(focusLog));
  ok(wmenu === wmenuNow && wmenu.open === openNow,
     "nav-details-untouched");
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_gone_starter_falls_back_to_h1(self):
        prog = self._prog(r'''
(async () => {
  document.activeElement = mkHeadBtn("repotab-retry");
  await diagRepo("demo");
  await tick();
  ok(wmenu.open === true, "retry-details-open");
  ok(focusLog[focusLog.length - 1] === "h1",
     "retry-fallback-h1:" + JSON.stringify(focusLog));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)


class L23bPendingFocusCase(unittest.TestCase):
    def _prog(self, extra):
        src = _script()
        decls = ["captureFk", "restoreFk", "focusViewH1",
                 "repoWmenuOpen", "setRepoWmenuOpen",
                 "restoreDiagFocus",
                 "captureRepoTabFocus", "restoreRepoTabFocus", "showRepoTab",
                 "diagRepo"]
        return (OK_STUB + L23_STUBS
                + "\n".join(_decl(src, n) for n in decls)
                + "\n" + extra)

    def test_pending_post_keeps_details_and_focus(self):
        prog = self._prog(r'''
(async () => {
  var release;
  postGate = new Promise(r => { release = r; });
  var done = diagRepo("demo");
  await tick();
  // The POST is still pending: the loading render must already
  // have restored the start-time state.
  ok(posts.length === 1, "pending-post-sent:" + posts.length);
  ok(renders === 1, "pending-one-render:" + renders);
  ok(wmenu.open === true, "pending-details-open");
  ok(focusLog[focusLog.length - 1] === "repohead-diag",
     "pending-focus-diagbtn:" + JSON.stringify(focusLog));
  release();
  await done;
  await tick();
  ok(posts.length === 1, "done-posts-once:" + posts.length);
  ok(wmenu.open === true, "done-details-open");
  ok(focusLog[focusLog.length - 1] === "repohead-diag",
     "done-focus-diagbtn:" + JSON.stringify(focusLog));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_moved_on_user_is_not_yanked_back(self):
        prog = self._prog(r'''
(async () => {
  var release;
  postGate = new Promise(r => { release = r; });
  var done = diagRepo("demo");
  await tick();
  // The user tabs to the reviews tab and closes details mid-load.
  wmenu.open = false;
  document.activeElement = tabStrip._btn;
  var n0 = focusLog.length;
  release();
  await done;
  await tick();
  ok(wmenu.open === false, "moved-details-stay-closed");
  ok(focusLog[focusLog.length - 1] === "tab:reviews",
     "moved-focus-tab:" + JSON.stringify(focusLog));
  ok(focusLog.slice(n0).indexOf("repohead-diag") === -1,
     "moved-no-yank:" + JSON.stringify(focusLog.slice(n0)));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_body_at_completion_moves_no_focus(self):
        prog = self._prog(r'''
(async () => {
  var release;
  postGate = new Promise(r => { release = r; });
  var done = diagRepo("demo");
  await tick();
  document.activeElement = {tagName: "BODY"};
  var n0 = focusLog.length;
  release();
  await done;
  await tick();
  ok(focusLog.length === n0,
     "body-no-move:" + JSON.stringify(focusLog.slice(n0)));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_pending_nav_moves_no_focus(self):
        prog = self._prog(r'''
(async () => {
  var release;
  postGate = new Promise(r => { release = r; });
  var done = diagRepo("demo");
  await tick();
  view = {kind: "repo", id: "other"};
  var n0 = focusLog.length;
  var wmenuNow = wmenu;
  release();
  await done;
  await tick();
  ok(focusLog.length === n0,
     "nav-no-move:" + JSON.stringify(focusLog.slice(n0)));
  ok(wmenu === wmenuNow && wmenu.open === true, "nav-details-untouched");
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_waiting_on_h1_keeps_h1(self):
        # The tab-retry starter falls back to h1 at loading time; the
        # completion render must not drop that h1 focus to BODY.
        prog = self._prog(r'''
(async () => {
  document.activeElement = mkHeadBtn("repotab-retry");
  var release;
  postGate = new Promise(r => { release = r; });
  var done = diagRepo("demo");
  await tick();
  ok(focusLog[focusLog.length - 1] === "h1",
     "loading-fallback-h1:" + JSON.stringify(focusLog));
  document.activeElement = {tagName: "H1"};
  release();
  await done;
  await tick();
  ok(focusLog[focusLog.length - 1] === "h1",
     "done-keeps-h1:" + JSON.stringify(focusLog));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)


class L24aHeadFocusCase(unittest.TestCase):
    def test_quiet_poll_restores_header_focus_by_key(self):
        src = _script()
        prog = (OK_STUB + L23_STUBS
                + _decl(src, "captureFk") + "\n"
                + _decl(src, "restoreFk") + "\n" + r'''
document.activeElement = summaryEl;
var kept = captureFk();
ok(kept && kept.key === "repohead-info" && kept.id === "demo",
   "capture-key:" + JSON.stringify(kept));
// A quiet render swaps the nodes and drops focus to BODY.
summaryEl = mkHeadBtn("repohead-info");
document.activeElement = {tagName: "BODY"};
ok(restoreFk(kept) === true, "restore-ok");
ok(focusLog[focusLog.length - 1] === "repohead-info",
   "restore-focus:" + JSON.stringify(focusLog));
// A navigation meanwhile must never restore.
view = {kind: "command"};
var n0 = focusLog.length;
ok(restoreFk(kept) === false, "nav-no-restore");
ok(focusLog.length === n0, "nav-no-focus-move");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_header_controls_carry_stable_keys(self):
        body = _decl(_script(), "renderRepo")
        self.assertIn('data-dk="repohead"', body)
        self.assertIn('data-fk="repohead-info"', body)
        self.assertIn('data-fk="repohead-start"', body)
        self.assertIn('data-fk="repohead-diag"', body)
        self.assertIn('data-fk="repohead-del"', body)

    def test_quiet_block_captures_and_restores_fk(self):
        body = _decl(_script(), "applyOverview")
        self.assertIn("captureFk()", body)
        self.assertIn("restoreFk(fkKeep)", body)

    def test_tab_retry_buttons_carry_fk(self):
        body = _decl(_script(), "paintRepoWork")
        self.assertIn('data-fk="repotab-retry"', body)
        body = _decl(_script(), "paintRepoDeps")
        self.assertIn('data-fk="repotab-retry"', body)


DIGEST_STUBS = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
async function tick() { await new Promise(r => setTimeout(r, 20)); }
var view = {kind: "digest", date: ""};
var focusLog = [];
var retryWired = null;
function mkFocus(name) {
  return {focus: function() { focusLog.push(name); }};
}
var sets = 0;
var box = {
  _html: "",
  _fp: undefined,
  set innerHTML(h) { sets++; this._html = h; },
  get innerHTML() { return this._html; },
  setAttribute: function() {},
  removeAttribute: function() {},
  contains: function() { return true; },
  querySelector: function(sel) {
    sel = sel || "";
    var f = /data-fk="([^"]+)"/.exec(sel);
    if (f) {
      if (this._html.indexOf('data-fk="' + f[1] + '"') === -1) return null;
      return mkFocus(f[1]);
    }
    if (sel.indexOf(".inline-err") !== -1) {
      if (this._html.indexOf("inline-err") === -1) return null;
      return {textContent: this._html};
    }
    if (sel === "[data-digestdate]") {
      var m = /data-digestdate="([^"]*)"/.exec(this._html);
      return m ? {dataset: {digestdate: m[1]}} : null;
    }
    if (sel === "[data-digest-err]") {
      return (this._html.indexOf("data-digest-err") !== -1) ? {} : null;
    }
    if (sel === "#digest-retry") {
      if (this._html.indexOf('id="digest-retry"') === -1) return null;
      var btn = {};
      Object.defineProperty(btn, "onclick", {
        set: function(fn) { retryWired = fn; },
        get: function() { return retryWired; }
      });
      return btn;
    }
    if (sel === "h1") {
      return (this._html.indexOf("<h1>") !== -1) ? mkFocus("h1") : null;
    }
    if (sel === "h2") {
      return (this._html.indexOf("<h2>") !== -1) ? mkFocus("h2") : null;
    }
    return null;
  },
  querySelectorAll: function() { return []; }
};
var document = {activeElement: {tagName: "BODY"}};
function $(id) { return (id === "view") ? box : {textContent: ""}; }
var CSS = {escape: function(s) { return String(s); }};
var M1_VERDICT_KO = {}, M1_VERDICT_CLS = {};
var SUB_APIKEY_RE = /x^/, SUB_LOGIN_RE = /x^/;
var APIKEY_SET_KO = "", LOGIN_NEEDED_KO = "";
var FETCH_ERR_TEXT = {};
function focusViewHeading() {}
var jgetMode = "unknown";
var clockN = 0;
async function jget(url) {
  if (jgetMode === "throw") throw new Error("boom");
  if (url.indexOf("/api/digest?date=") === 0) return {unknown: true};
  if (jgetMode === "unknownClock") {
    clockN++;
    return {unknown: true, date: "", served_at: "t" + clockN};
  }
  if (jgetMode === "dataB") return {title: "B", date: "D", repos: []};
  if (jgetMode === "dataA") return {title: "A", date: "D", repos: []};
  return {unknown: true, date: ""};
}
'''


def _digest_prog(src, extra):
    decls = ["esc", "subErrKo", "fetchErrKo", "errBlockHtml",
             "errNewToBox",
             "digestPrevDay", "digestFp", "digestPaintedFor",
             "captureFk", "restoreFk", "focusViewH1",
             "jgetDedupe",
             "fmtKST", "fmtKSThm", "digestHeadHtml", "digestNeedHtml",
             "digestRepoHtml", "digestEmptyHtml",
             "renderDigestView"]
    inflight = next(line for line in src.splitlines()
                    if line.startswith("const inflightGets"))
    needko = next(line for line in src.splitlines()
                  if line.startswith("const DG_NEED_KO"))
    return (DIGEST_STUBS + inflight + "\n" + needko + "\n"
            + "\n".join(_decl(src, n) for n in decls)
            + "\n" + extra)


class L24bDigestNoopCase(unittest.TestCase):
    def test_unchanged_fingerprint_leaves_dom_alone(self):
        prog = _digest_prog(_script(), r'''
(async () => {
  await renderDigestView("");
  await tick();
  ok(sets >= 1, "first-paints:" + sets);
  var painted = sets;
  var ae = mkFocus("digest-glance");
  ae.dataset = {fk: "digest-glance"};
  document.activeElement = ae;
  focusLog = [];
  await renderDigestView("");
  await tick();
  ok(sets === painted, "quiet-no-repaint:" + sets + "/" + painted);
  ok(document.activeElement === ae, "quiet-focus-kept");
  ok(focusLog.length === 0, "quiet-no-focus-move");
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_served_at_clock_alone_is_not_a_change(self):
        # /api/digest stamps a fresh served_at per request; the
        # fingerprint must ignore that clock (batches.py precedent:
        # repaint compares semantic content with clocks stripped).
        prog = _digest_prog(_script(), r'''
(async () => {
  jgetMode = "unknownClock";
  await renderDigestView("");
  await tick();
  var painted = sets;
  ok(clockN >= 1, "clock-ticked:" + clockN);
  await renderDigestView("");
  await tick();
  ok(clockN >= 2, "clock-ticked-again:" + clockN);
  ok(sets === painted, "clock-no-repaint:" + sets + "/" + painted);
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_changed_digest_repaints_and_restores_by_key(self):
        prog = _digest_prog(_script(), r'''
(async () => {
  jgetMode = "dataA";
  await renderDigestView("");
  await tick();
  var painted = sets;
  var ae = {dataset: {fk: "digest-glance"}};
  document.activeElement = ae;
  focusLog = [];
  jgetMode = "dataB";
  await renderDigestView("");
  await tick();
  ok(sets > painted, "changed-repaints:" + sets + "/" + painted);
  ok(box.innerHTML.indexOf(">B<") !== -1, "changed-content");
  ok(focusLog[focusLog.length - 1] === "digest-glance",
     "changed-focus-key:" + JSON.stringify(focusLog));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_first_render_still_shows_loading(self):
        prog = _digest_prog(_script(), r'''
(async () => {
  var sawSkeleton = false;
  var desc = Object.getOwnPropertyDescriptor(box, "innerHTML");
  Object.defineProperty(box, "innerHTML", {
    set: function(h) {
      sets++;
      if (String(h).indexOf("하루 요약을 불러오는 중이에요") !== -1) {
        sawSkeleton = true;
      }
      desc.set.call(box, h);
    },
    get: desc.get
  });
  await renderDigestView("");
  await tick();
  ok(sawSkeleton, "first-loading-shown");
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)


class L15bDigestErrorCase(unittest.TestCase):
    def test_error_alerts_once_and_offers_retry(self):
        prog = _digest_prog(_script(), r'''
(async () => {
  jgetMode = "throw";
  await renderDigestView("");
  await tick();
  ok(box.innerHTML.indexOf('id="digest-retry"') !== -1, "err-has-retry");
  ok(box.innerHTML.indexOf("다시 시도") !== -1, "err-retry-ko");
  ok(box.innerHTML.indexOf("한눈에 보기로") !== -1, "err-keeps-glance");
  ok(box.innerHTML.indexOf('role="alert"') !== -1, "err-first-alert");
  // An identical poll repeat must not re-announce.
  await renderDigestView("");
  await tick();
  ok(box.innerHTML.indexOf('role="alert"') === -1, "err-repeat-quiet");
  // The wired retry re-runs for the same date and paints data.
  jgetMode = "dataA";
  document.activeElement = {dataset: {fk: "digest-retry"}};
  ok(typeof retryWired === "function", "err-retry-wired");
  await retryWired();
  await tick();
  ok(box.innerHTML.indexOf(">A<") !== -1, "err-retry-paints");
  ok(focusLog[focusLog.length - 1] === "h1",
     "err-retry-h1:" + JSON.stringify(focusLog));
  console.log("PASS-COUNT " + pass);
})();
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)
