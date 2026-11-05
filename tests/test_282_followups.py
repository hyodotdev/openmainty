"""Slice 282: skip link keeps the route (L27), runs names (L26).

L27: activating the skip link never changes location.hash; focus
moves to the view heading (h1, else h2, else #view). A bare #view
hash keeps the current route instead of falling back to #/command.
L26: every 상세 link on #/runs has a unique aria-label with its run.
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


SKIP_STUBS = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
var focusLog = [];
function mkHead(tag, name) {
  var h = {tagName: tag,
           focus: function() {
             focusLog.push(name);
             document.activeElement = h;
           }};
  return h;
}
var h1El = mkHead("H1", "h1");
var h2El = mkHead("H2", "h2");
var viewEl = {
  tagName: "MAIN", tabIndex: 0,
  focus: function() {
    focusLog.push("view");
    document.activeElement = viewEl;
  },
  querySelector: function(sel) {
    if (sel === "h1") return h1El;
    if (sel === "h2") return h2El;
    return null;
  }
};
var document = {activeElement: {tagName: "A"}};
function $(id) { return (id === "view") ? viewEl : null; }
var location = {hash: "#/digest"};
var view = {kind: "digest", date: ""};
'''


class L27SkipLinkCase(unittest.TestCase):
    def _prog(self, extra):
        src = _script()
        decls = ["focusViewH1", "skipToView", "skipScrollTarget",
                 "parseHash"]
        return (SKIP_STUBS + "\n".join(_decl(src, n) for n in decls)
                + "\n" + extra)

    def test_skip_activation_keeps_hash_and_focuses_heading(self):
        prog = self._prog(r'''
var prevented = false;
var ret = skipToView({preventDefault: function() { prevented = true; }});
ok(prevented === true, "skip-prevents-default");
ok(ret === false, "skip-returns-false");
ok(location.hash === "#/digest", "skip-hash-kept:" + location.hash);
ok(focusLog[focusLog.length - 1] === "h1",
   "skip-focus-h1:" + JSON.stringify(focusLog));
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_skip_falls_back_to_h2_then_view(self):
        prog = self._prog(r'''
h1El = null;
skipToView({preventDefault: function() {}});
ok(focusLog[focusLog.length - 1] === "h2",
   "skip-fallback-h2:" + JSON.stringify(focusLog));
h2El = null;
focusLog = [];
skipToView({preventDefault: function() {}});
ok(focusLog[focusLog.length - 1] === "view",
   "skip-fallback-view:" + JSON.stringify(focusLog));
ok(viewEl.tabIndex === -1, "skip-view-tabindex");
ok(location.hash === "#/digest", "skip-hash-kept:" + location.hash);
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_bare_view_hash_keeps_current_route(self):
        prog = self._prog(r'''
location.hash = "#view";
var v = parseHash();
ok(JSON.stringify(v) === JSON.stringify(view),
   "view-hash-keeps:" + JSON.stringify(v));
// Sanity: real routes still parse.
location.hash = "#/command";
ok(parseHash().kind === "command", "command-kept");
location.hash = "#/glance";
ok(parseHash().kind === "glance", "glance-kept");
location.hash = "";
ok(parseHash().kind === "glance", "empty-kept");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_skip_link_wired_and_hashchange_guards_view(self):
        src = _script()
        self.assertIn('class="skip" href="#view"', src)
        self.assertIn("skipToView", src)
        # The hashchange listener restores the address bar when a
        # bare #view hash arrives instead of routing anywhere.
        self.assertIn('=== "#view"', src)


RUNS_STUBS = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
function esc(s) { return String(s ?? ""); }
function pillKo(s) { return "[" + s + "]"; }
function kindKo(k) { return k; }
function waitKo(w) { return w || ""; }
function settledWatchPillFor() { return null; }
function latestPerIssue() { return {}; }
function focusViewHeading() {}
function detail() {}
function openWork() {}
function cancelJob() {}
var selRun = null;
var view = {kind: "runs"};
var runsShown = 30, runsShownSig = "", runsFocusKey = null, runsLastIds = [];
var boxHtml = "";
var document = {querySelectorAll: function() { return []; }};
function $(id) {
  if (id === "view") {
    return {set innerHTML(h) { boxHtml = h; },
            get innerHTML() { return boxHtml; }};
  }
  return {textContent: ""};
}
var O = {jobs: [], watch_log_tail: "",
         runs: [
           {id: "run-aaa", repo: "r", issue: "1", title: "t",
            steps: {}, waitReason: ""},
           {id: "run-bbb", repo: "r", issue: "2", title: "t",
            steps: {}, waitReason: ""}
         ]};
'''


RUNS_DEPS = ["runStageSummary", "runsMatchState", "runsCountByState",
             "runsRepos", "runsFilter", "runsFilterSig", "runsViewFilter",
             "runsChipsHtml", "runsRepoSelHtml", "runsCountHtml",
             "runsRowHtml", "runsNarrow", "runsApplyFocus"]


class L26RunsNamesCase(unittest.TestCase):
    def test_detail_links_named_with_run_id(self):
        src = _script()
        deps = "".join(_decl(src, name) + "\n" for name in RUNS_DEPS)
        prog = (RUNS_STUBS + "var RUNS_PAGE = 30;\n" + deps
                + _decl(src, "renderRuns") + r'''
renderRuns();
var names = [];
var re = /<a [^>]*data-work="([^"]+)"[^>]*aria-label="([^"]+)"[^>]*>상세<\/a>/g;
var m;
while ((m = re.exec(boxHtml)) !== null) {
  names.push([m[1], m[2]]);
}
ok(names.length === 2, "two-named-links:" + names.length);
ok(names[0][1].indexOf("run-aaa") !== -1,
   "first-name:" + JSON.stringify(names));
ok(names[1][1].indexOf("run-bbb") !== -1,
   "second-name:" + JSON.stringify(names));
ok(names[0][1] !== names[1][1], "names-unique");
ok(boxHtml.indexOf(">상세</a>") !== -1, "visible-text-kept");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)
