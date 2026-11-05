"""Gate-3 live-check followups (243): L2 mobile zero-state, L4 focus,
L3 single action link, L1 graph poll guards.

Dashboard fixes are verified against the shipped
``src/openmainty/dashboard.html`` inline script: real functions are
extracted and executed under node with stub DOM (as in
tests/test_graph_poll_ui_logic.py); render-site wiring that cannot run
without layout is guarded by structural asserts, and visible behavior
is verified in a sandboxed demo browser pass with measurements.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"


def _script():
    text = HTML.read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline dashboard script"
    return blocks[0]


def _fn_body(src, name):
    """Balanced-brace body of a top-level (async) function."""
    m = re.search(r"(?:^|\n)(?:async\s+)?function\s+" + re.escape(name)
                  + r"\s*\(", src)
    assert m, f"missing function {name}"
    i = src.index("{", m.start())
    depth, q, line, block, esc = 0, None, False, False, False
    for j in range(i, len(src)):
        c = src[j]
        n = src[j + 1] if j + 1 < len(src) else ""
        if line:
            if c == "\n":
                line = False
            continue
        if block:
            if c == "*" and n == "/":
                block = False
            continue
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = None
            continue
        if c == "/" and n == "/":
            line = True
            continue
        if c == "/" and n == "*":
            block = True
            continue
        if c in ("'", '"', "`"):
            q = c
            continue
        if c == "{":
            depth += 1
        if c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"unbalanced function {name}")


def _decl(src, name):
    m = re.search(
        r"(?:^|\n)((?:async\s+)?function\s+" + re.escape(name)
        + r"\s*\([^)]*\))",
        src,
    )
    assert m, f"missing function {name}"
    return m.group(1) + " " + _fn_body(src, name)


def _run_node(script):
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


L2_FNS = [
    "paintGraph",
    "setGraphZero",
    "setGraphMsg",
    "setRelationHint",
    "setGraphLimits",
    "validGraphSel",
    "graphTruncated",
    "graphLimitsNotice",
    "graphFiltered",
    "graphEmptyKind",
    "graphSearchHits",
    "graphSearchPage",
    "paintSearchPager",
    "clearGraphSearch",
    "clearGraphFilters",
]

L2_STUBS = r'''
var graphCache = null, graphSel = null, gDetailKey = null;
var gQuery = "", gClientType = "", gClientStatus = "";
var gMode = "overview", gFocus = null;
var gDepMembers = {}, gAggMembers = {};
var gSearchPage = 0, gSearchPages = 1;
var view = {kind: "graph", repo: ""};
var O = {repos: []};
function syncHash() {}
function graphColor() { return "#000"; }
function graphCanvasW() { return 640; }
function jpost() { return Promise.resolve({}); }
function loadAll() { return Promise.resolve(); }
function toast() {}
var fitCalls = 0;
function fitGraphChrome() { fitCalls++; }
var gZeroNotice = false;
var gRelHintMsg = null, gRelHintEl = null;
var gLimitsMsg = null, gLimitsEl = null;
function mkEl(id) {
  var el = {
    id: id, innerHTML: "", textContent: "", value: "", onclick: null,
    dataset: {}, style: {}, _attrs: {}, _cls: {},
    setAttribute: function (k, v) { this._attrs[k] = v; },
    getAttribute: function (k) { return (k in this._attrs) ? this._attrs[k] : null; },
    removeAttribute: function (k) { delete this._attrs[k]; },
    querySelector: function (sel) {
      if (!this._qs) this._qs = {};
      if (!this._qs[sel]) this._qs[sel] = mkEl(this.id + sel);
      return this._qs[sel];
    },
    querySelectorAll: function () { return []; }
  };
  el.classList = {
    toggle: function (k, f) { el._cls[k] = (f === undefined) ? !el._cls[k] : !!f; },
    add: function (k) { el._cls[k] = true; },
    remove: function (k) { delete el._cls[k]; },
    contains: function (k) { return !!el._cls[k]; }
  };
  return el;
}
var __lay = mkEl("glayout");
var document = {
  querySelector: function (sel) { return sel === ".glayout" ? __lay : null; }
};
var __els = {};
function $(id) {
  if (!__els[id]) __els[id] = mkEl(id);
  return __els[id];
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function resetZeroHarness() {
  for (const k in __els) delete __els[k];
  __lay = mkEl("glayout");
  fitCalls = 0;
  graphCache = null; graphSel = null; gDetailKey = null;
  gQuery = ""; gClientType = ""; gClientStatus = "";
}
function layHas(cls) { return !!__lay._cls[cls]; }
''';

L2_CHECKS = r'''
// A) type-filter zero marks the layout so narrow CSS can fold the canvas.
resetZeroHarness();
graphCache = {nodes: [{id: "n1", type: "issue", label: "a"}], edges: []};
gClientType = "run";
paintGraph();
ok(layHas("gzero"), "filtered-zero sets gzero on .glayout");
ok($("grelhint").textContent === "" && $("grelhint")._cls["hidden"] === true,
  "filtered-zero clears the relation hint");
ok(fitCalls > 0, "filtered-zero refreshes panel visibility");
ok(typeof $("g-clearf").onclick === "function", "filtered-zero keeps a filter clear");
// B) search zero marks the layout and keeps the clear-search action.
resetZeroHarness();
graphCache = {nodes: [{id: "n1", type: "issue", label: "슬라이더 고장"}], edges: []};
gQuery = "zzz-no-match";
paintGraph();
ok(layHas("gzero"), "search-zero sets gzero on .glayout");
ok($("grelhint").textContent === "" && $("grelhint")._cls["hidden"] === true,
  "search-zero clears the relation hint");
ok(fitCalls > 0, "search-zero refreshes panel visibility");
ok($("gdetail").innerHTML.indexOf("일치하는 기록 없음") !== -1,
  "search-zero keeps the no-result message");
ok(typeof $("g-clearq").onclick === "function", "search-zero keeps a clear action");
// C) genuinely empty cache marks the layout and keeps the scan route.
resetZeroHarness();
graphCache = {nodes: [], edges: []};
paintGraph();
ok(layHas("gzero"), "true empty sets gzero on .glayout");
ok($("grelhint").textContent === "" && $("grelhint")._cls["hidden"] === true,
  "true empty clears the relation hint");
ok(fitCalls > 0, "true empty refreshes panel visibility");
ok($("gdetail").innerHTML.indexOf("g-scan") !== -1, "true empty keeps the scan CTA");
console.log("PASS " + pass);
''';


class L2ZeroCanvasTest(unittest.TestCase):
    def test_zero_states_mark_glayout_gzero(self):
        src = _script()
        decls = "\n".join(_decl(src, n) for n in L2_FNS)
        type_line = next(
            line for line in src.splitlines()
            if line.startswith("const TYPE_KO = ")
        )
        esc_line = next(
            line for line in src.splitlines()
            if line.startswith("function esc(")
        )
        assert esc_line.rstrip().endswith("}"), \
            "esc must stay a single-line declaration"
        page_line = next(
            line for line in src.splitlines()
            if line.startswith("const SEARCH_PAGE_N = ")
        )
        script = (L2_STUBS + "\n" + type_line + "\n" + esc_line + "\n"
                  + page_line + "\n" + decls + "\n" + L2_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L2 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_zero_and_error_paths_refresh_chrome(self):
        src = _script()
        # Every early return that leaves a notice+button in the panel
        # must re-run fitGraphChrome: a panel hidden by a previous
        # state would otherwise cover the new notice on narrow screens.
        self.assertEqual(
            _fn_body(src, "paintGraph").count("fitGraphChrome();"), 4)
        self.assertEqual(
            _fn_body(src, "renderGraphView").count("fitGraphChrome();"), 2)

    def test_main_path_clears_gzero(self):
        paint = _fn_body(_script(), "paintGraph")
        anchor = "nodes = show; edges = showE;"
        self.assertIn(anchor, paint)
        tail = paint[paint.index(anchor):]
        self.assertIn("setGraphZero(false)", tail,
                      "data path must clear the zero mark")

    def test_narrow_css_hides_zero_canvas_only(self):
        text = HTML.read_text()
        style = text[text.index("<style>"):text.index("</style>")]
        self.assertIn(".glayout.gzero .gcanvas", style)
        # The zero-canvas rule lives inside a narrow (<=820px) media
        # block and nowhere else: desktop keeps the canvas.
        self.assertEqual(style.count(".glayout.gzero"), 1)
        at = style.index(".glayout.gzero .gcanvas")
        media = style[:at].rfind("@media")
        self.assertGreaterEqual(media, 0, "zero rule must sit in a media block")
        self.assertIn("max-width: 820px",
                      style[media:media + 60])
        self.assertIn("display: none",
                      style[at:at + 120])


L4_STUBS = r'''
var view = {kind: "work", id: "run-1"};
var repoTab = "overview";
var lastRouteKey = null, lastRenderResetScroll = false;
var pendingViewFocusKey = null, bootRenderDone = true;
var __h1 = null;
function mkH1() {
  return {tabIndex: 0, focusArgs: "unset",
    focus: function (o) { this.focusArgs = o; }};
}
var __viewEl = {querySelector: function (sel) {
  return sel === "h1" ? __h1 : null; }};
function $(id) { return id === "view" ? __viewEl : null; }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function resetFocusHarness() {
  __h1 = mkH1();
  pendingViewFocusKey = null;
  bootRenderDone = true;
  view = {kind: "work", id: "run-1"};
}
''';

L4_CHECKS = r'''
// A) armed on the current route: h1 gets tabindex=-1 and preventScroll focus, one shot.
resetFocusHarness();
pendingViewFocusKey = routeKey();
eq(focusViewHeading(), true, "armed focus succeeds");
eq(__h1.tabIndex, -1, "h1 takes tabindex -1");
eq(__h1.focusArgs && __h1.focusArgs.preventScroll, true, "focus uses preventScroll");
eq(focusViewHeading(), false, "focus is one shot per navigation");
// B) stale route key: no focus, and the stale arm is dropped.
resetFocusHarness();
pendingViewFocusKey = "command||";
eq(focusViewHeading(), false, "stale arm does not focus");
eq(__h1.focusArgs, "unset", "stale arm never touches the h1");
eq(pendingViewFocusKey, null, "stale arm is dropped");
// C) unarmed (quiet re-render): never steals focus.
resetFocusHarness();
eq(focusViewHeading(), false, "quiet render does not focus");
eq(__h1.focusArgs, "unset", "quiet render never touches the h1");
// D) armed but no h1 in the view: no crash, arm consumed.
resetFocusHarness();
__h1 = null;
pendingViewFocusKey = routeKey();
eq(focusViewHeading(), false, "missing h1 reports false");
eq(pendingViewFocusKey, null, "missing h1 still consumes the arm");
console.log("PASS " + pass);
''';


class L4ViewFocusTest(unittest.TestCase):
    def test_focus_view_heading_logic(self):
        src = _script()
        decls = "\n".join(_decl(src, n)
                          for n in ("focusViewHeading", "routeKey"))
        proc = _run_node(L4_STUBS + "\n" + decls + "\n" + L4_CHECKS)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L4 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_double_render_keeps_arm_until_paint(self):
        src = _script()
        decls = "\n".join(_decl(src, n)
                          for n in ("armViewFocus", "focusViewHeading",
                                    "routeKey"))
        checks = r'''
// render #1 arms on the route change; render #2 (palette double
// render, same route) must not clear it before the async paint.
resetFocusHarness();
view = {kind: "work", id: "run-1"};
armViewFocus(true);
armViewFocus(false);
eq(focusViewHeading(), true, "double render keeps the arm until paint");
eq(__h1.tabIndex, -1, "painted h1 takes tabindex -1");
eq(__h1.focusArgs && __h1.focusArgs.preventScroll, true,
  "painted h1 focuses without scrolling");
// a stale arm for another route is dropped on a same-route render.
resetFocusHarness();
view = {kind: "runs"};
pendingViewFocusKey = "work|run-1||";
armViewFocus(false);
eq(pendingViewFocusKey, null, "stale arm is dropped");
eq(focusViewHeading(), false, "dropped arm never focuses");
// the boot render never arms.
resetFocusHarness();
bootRenderDone = false;
view = {kind: "command"};
armViewFocus(true);
eq(pendingViewFocusKey, null, "boot render does not arm");
console.log("PASS " + pass);
'''
        proc = _run_node(L4_STUBS + "\n" + decls + "\n" + checks)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L4 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_render_arms_focus_only_on_route_change(self):
        src = _script()
        render = _fn_body(src, "render")
        self.assertIn("armViewFocus(lastRenderResetScroll);", render)
        arm = _fn_body(src, "armViewFocus")
        self.assertIn("if (routeChanged && bootRenderDone)", arm)
        self.assertIn("else if (pendingViewFocusKey !== routeKey())",
                      arm)

    def test_every_h1_view_consumes_focus_once_painted(self):
        src = _script()
        for name in ("renderGlanceView", "renderDigestView",
                     "renderCommand", "renderRepo", "renderRuns",
                     "renderWorkView", "renderGraphView", "renderEmpty"):
            with self.subTest(renderer=name):
                self.assertIn("focusViewHeading();", _fn_body(src, name),
                              f"{name} must focus the h1 once painted")

    def test_quiet_rerender_preserves_view_h1_focus(self):
        apply = _fn_body(_script(), "applyOverview")
        self.assertIn('document.activeElement.tagName === "H1"', apply)
        self.assertIn("if (h1WasFocused && !lastRenderResetScroll) {",
                      apply)

    def test_legend_swatches_are_decorative(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        self.assertIn('aria-hidden="true">●</', paint)
        self.assertNotIn('>●</span>${ko}', paint.replace(
            'aria-hidden="true">●</span>${ko}', ""))


L3_STUBS = r'''
var O = {repos: [{name: "r1", github: "o/r1"}]};
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function countLinks(out) { return out.split("<a ").length - 1; }
''';

L3_CHECKS = r'''
// A) dep-like list: names as plain text plus ONE 44px action link.
var out = eventDetailHtml(
  {repo: "r1", detail: "pkg-a->1.0.0; pkg-b->2.0.0; pkg-c->3.0.0"});
ok(out.indexOf("항목 3건") !== -1, "dep list keeps its item count");
ok(out.indexOf("pkg-a") !== -1 && out.indexOf("pkg-b") !== -1 &&
  out.indexOf("pkg-c") !== -1, "names stay readable as text");
eq(countLinks(out), 1, "dep list renders exactly one link");
eq(out.split("?tab=deps").length - 1, 1, "one deps destination per list");
ok(out.indexOf('class="alink"') !== -1, "the single link is a 44px alink");
ok(out.indexOf("의존성에서 보기") !== -1, "the single link names its target");
// B) no repo: no links at all, names still listed.
var bare = eventDetailHtml(
  {repo: "", detail: "pkg-a->1.0.0; pkg-b->2.0.0"});
eq(countLinks(bare), 0, "repo-less list renders no links");
ok(bare.indexOf("pkg-a") !== -1, "repo-less list keeps names");
// C) prose detail keeps the verbatim path with no deps link.
var prose = eventDetailHtml({repo: "r1", detail: "스캔 완료, 이상 없음"});
ok(prose.indexOf("원문") !== -1, "prose keeps the verbatim label");
eq(prose.split("?tab=deps").length - 1, 0, "prose adds no deps link");
console.log("PASS " + pass);
''';


class L3SingleDepsLinkTest(unittest.TestCase):
    def test_dep_list_renders_one_action_link(self):
        src = _script()
        decls = "\n".join(_decl(src, n)
                          for n in ("eventDetailHtml", "ghIssueUrl"))
        esc_line = next(
            line for line in src.splitlines()
            if line.startswith("function esc(")
        )
        assert esc_line.rstrip().endswith("}"), \
            "esc must stay a single-line declaration"
        script = (L3_STUBS + "\n" + esc_line + "\n" + decls
                  + "\n" + L3_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L3 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L1ClientWiringTest(unittest.TestCase):
    def test_tick_poll_has_no_force(self):
        src = _script()
        # The 30s general tick must stay coolable: only the explicit
        # retry bypasses the cool (behavior pinned by the extended
        # poll driver in tests/test_graph_poll_ui_logic.py).
        apply = _fn_body(src, "applyOverview")
        self.assertIn("graphPollRefresh();", apply)
        self.assertNotIn("graphPollRefresh(true)", apply)

    def test_retry_bypasses_cool(self):
        warn = _fn_body(_script(), "setGraphWarning")
        self.assertIn("graphPollRefresh(true);", warn)

    def test_navigation_recools(self):
        view = _fn_body(_script(), "renderGraphView")
        self.assertIn("gPollCoolUntil = Date.now() + G_POLL_COOL_MS;",
                      view)


if __name__ == "__main__":
    unittest.main()
