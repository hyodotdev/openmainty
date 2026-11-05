"""Gate-3b live-check followups (245): F-UI-5 stale panel, L1b/L1c
graph load cost, L6 node hit target, L7 sidebar breakpoint, L5 dup GETs.

Same strategy as tests/test_243_gate3.py: real dashboard functions
extracted and executed under node with stub DOM; render-site wiring
that cannot run without layout is guarded by structural asserts, and
visible behavior is verified in a sandboxed demo browser pass with
measurements.
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


FUI5_STUBS = r'''
var graphSel = null, gZeroNotice = false;
var gMode = "overview", gQuery = "";
var __panelHtml = "", __panelSkel = null, __paintedGid = "unset";
function paintGDetail(gid) { __paintedGid = gid; }
function $(id) {
  if (id !== "gdetail") return null;
  return {
    get innerHTML() { return __panelHtml; },
    set innerHTML(v) { __panelHtml = v; },
    querySelector: function (sel) {
      return sel === ".skel" ? __panelSkel : null;
    }
  };
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function resetPanelHarness() {
  graphSel = null; gZeroNotice = false;
  gMode = "overview"; gQuery = "";
  __panelHtml = ""; __panelSkel = null; __paintedGid = "unset";
}
''';

FUI5_CHECKS = r'''
// A) a live selection always wins: its detail paints, the panel flag
//    is left for the real painter to clear.
resetPanelHarness();
graphSel = "n1"; gZeroNotice = true;
__panelHtml = '검색 "zz" — 일치하는 기록 없음';
paintGraphPanel();
eq(__paintedGid, "n1", "selection repaints its detail");
// B) no selection + stale zero notice -> default guidance, flag down.
resetPanelHarness();
gZeroNotice = true; gQuery = "nitro";
__panelHtml = '검색 "zzqqxx" — 일치하는 기록 없음';
paintGraphPanel();
ok(__panelHtml.indexOf("일치하는 기록 없음") === -1,
  "successful paint replaces the zero notice");
ok(__panelHtml.indexOf("노드를 클릭하면") !== -1,
  "zero notice falls back to the default guidance");
eq(gZeroNotice, false, "replaced notice clears the flag");
// C) overview without a query keeps the bundle guidance variant.
resetPanelHarness();
gZeroNotice = true;
__panelHtml = "표시할 기록 없음";
paintGraphPanel();
ok(__panelHtml.indexOf("묶음을 누르면") !== -1,
  "overview reset keeps the bundle guidance");
// D) real content without the flag is never touched.
resetPanelHarness();
__panelHtml = "노드 근거 본문";
paintGraphPanel();
eq(__panelHtml, "노드 근거 본문", "genuine panel content is preserved");
eq(__paintedGid, "unset", "no selection paints nothing");
// E) skeleton still resets (pre-existing behavior, kept).
resetPanelHarness();
__panelSkel = {};
__panelHtml = "loading";
paintGraphPanel();
ok(__panelHtml.indexOf("묶음을 누르면") !== -1, "skeleton resets");
console.log("PASS " + pass);
''';


class FUi5PanelResetTest(unittest.TestCase):
    def test_successful_paint_replaces_zero_notice(self):
        src = _script()
        script = (FUI5_STUBS + "\n" + _decl(src, "paintGraphPanel")
                  + "\n" + FUI5_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"F-UI-5 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_zero_branches_mark_and_paint_calls_reset(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        self.assertEqual(paint.count("gZeroNotice = true;"), 3,
                         "all three zero branches must mark the notice")
        self.assertIn("paintGraphPanel();", paint,
                      "successful paint must run the panel reset")
        for name in ("paintGDetail", "clearGraphPanel"):
            with self.subTest(writer=name):
                self.assertIn("gZeroNotice = false;",
                              _fn_body(src, name),
                              f"{name} writes real content, so clears the mark")


class L6HitTargetTest(unittest.TestCase):
    # Live 243c at 1280 renders g.nd role=button 43px tall although the
    # template rect is 44: the CSS width can settle ~2.3% under the
    # viewBox width measured at paint time (43/44 = 0.977), shrinking
    # every node. The invisible hit rect must therefore be larger than
    # 44 so the RENDERED target stays >= 44px: 44/0.977 = 45.04, and 48
    # is chosen for margin (covers scale >= 0.917) while still fitting
    # the 62px row pitch. Visuals are untouched (transparent rect).
    LIVE_SCALE = 43 / 44

    def _hit_rect(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        m = re.search(
            r'x="\$\{n\._x - (\d+)\}" y="\$\{n\._y - (\d+)\}" '
            r'width="(\d+)" height="(\d+)" fill="transparent"',
            paint,
        )
        self.assertIsNotNone(m, "R2 invisible hit rect missing from paintGraph")
        ox, oy, w, h = (int(m.group(i)) for i in range(1, 5))
        return ox, oy, w, h

    def test_hit_rect_survives_live_shrink(self):
        ox, oy, w, h = self._hit_rect()
        self.assertEqual(w, h, "hit rect must stay square")
        self.assertGreaterEqual(
            min(w, h) * self.LIVE_SCALE, 44,
            f"{w}x{h} renders below 44px at the live 0.977 scale")
        self.assertEqual(ox, w // 2, "rect must stay centred on the node")
        self.assertEqual(oy, h // 2, "rect must stay centred on the node")

    def test_hit_rect_fits_row_pitch(self):
        _, _, w, h = self._hit_rect()
        src = _script()
        paint = _fn_body(src, "paintGraph")
        m = re.search(r"ROWH = (\d+)", paint)
        self.assertIsNotNone(m, "ROWH pitch missing from paintGraph")
        self.assertLess(max(w, h), int(m.group(1)),
                       "hit rect must not overlap the neighbouring row")


L7_STUBS = r'''
var __sideClasses = {}, __backdropClasses = {};
function __cls(store) {
  return {
    add: function (c) { store[c] = 1; },
    remove: function (c) { delete store[c]; },
    contains: function (c) { return !!store[c]; }
  };
}
function $(id) {
  if (id === "side") return { classList: __cls(__sideClasses) };
  if (id === "sidebackdrop") return { classList: __cls(__backdropClasses) };
  if (id === "sidetoggle") return { focus: function () {} };
  return null;
}
function syncSideToggle() {}
let gSideNarrow = false;
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L7_CHECKS = r'''
// A) stale drawer state + crossing into narrow -> closed, like a
//    fresh narrow load (no backdrop, no .open, no focus steal).
__sideClasses = {open: 1}; __backdropClasses = {};
eq(sideBreakpointCross(true), true, "crossing into narrow is detected");
ok(!__sideClasses.open, "crossing into narrow closes the stale drawer");
ok(!!__backdropClasses.hidden, "backdrop is hidden after the crossing");
// B) resize inside one mode keeps a user-opened drawer.
__sideClasses = {open: 1};
eq(sideBreakpointCross(true), false, "same-side resize is not a crossing");
ok(!!__sideClasses.open, "in-mode resize preserves the drawer");
// C) crossing back to wide clears the drawer state too.
eq(sideBreakpointCross(false), true, "crossing into wide is detected");
ok(!__sideClasses.open, "crossing into wide clears the drawer state");
// D) a clean crossing with no stale state is harmless.
eq(sideBreakpointCross(true), true, "clean crossing is still a crossing");
console.log("PASS " + pass);
''';


class L7SidebarBreakpointTest(unittest.TestCase):
    def test_crossing_resets_drawer(self):
        src = _script()
        script = (L7_STUBS + "\n" + _decl(src, "closeSide")
                  + "\n" + _decl(src, "sideBreakpointCross")
                  + "\n" + L7_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L7 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_resize_listener_calls_crossing(self):
        src = _script()
        anchor = ('if (!window.matchMedia("(max-width: 820px)").matches)\n'
                  '    $("sidebackdrop").classList.add("hidden");')
        i = src.find(anchor)
        self.assertNotEqual(i, -1, "sidebar resize listener missing")
        j = src.rfind("sideBreakpointCross(window.matchMedia", 0, i)
        self.assertNotEqual(j, -1,
                            "resize listener must run the crossing reset")
        self.assertLess(i - j, 400,
                        "crossing call must sit inside the resize listener")


L5_STUBS = r'''
let gWorkFetch = null;
let __jgetCalls = 0;
let __jgetDeferred = null;
function jget(url) {
  __jgetCalls++;
  __jgetDeferred = {};
  __jgetDeferred.promise = new Promise((res, rej) => {
    __jgetDeferred.res = res; __jgetDeferred.rej = rej;
  });
  return __jgetDeferred.promise;
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L5_CHECKS = r'''
(async () => {
  // A) concurrent opens of the same run share one GET.
  const p1 = workFetchJoin("r1");
  const p2 = workFetchJoin("r1");
  eq(__jgetCalls, 1, "one in-flight GET per run");
  __jgetDeferred.res({run: "r1"});
  eq((await p1).run, "r1", "first joiner gets the data");
  eq((await p2).run, "r1", "second joiner gets the same data");
  // B) a different run fetches separately.
  const p3 = workFetchJoin("r2");
  eq(__jgetCalls, 2, "different run starts its own GET");
  __jgetDeferred.res({run: "r2"});
  eq((await p3).run, "r2", "other run resolves");
  // C) after settle the next open fetches fresh (no stale cache).
  await new Promise(r => setTimeout(r, 10));
  const p4 = workFetchJoin("r1");
  eq(__jgetCalls, 3, "settled guard fetches fresh");
  __jgetDeferred.res({run: "r1"});
  await p4;
  // D) errors reach every joiner and clear the guard.
  await new Promise(r => setTimeout(r, 10));
  const p5 = workFetchJoin("r3");
  const p6 = workFetchJoin("r3");
  eq(__jgetCalls, 4, "error case still shares one GET");
  __jgetDeferred.rej(new Error("boom"));
  let e1 = "", e2 = "";
  await p5.catch(e => { e1 = e.message; });
  await p6.catch(e => { e2 = e.message; });
  eq(e1, "boom", "first joiner sees the error");
  eq(e2, "boom", "second joiner sees the error");
  await new Promise(r => setTimeout(r, 10));
  const p7 = workFetchJoin("r3");
  p7.catch(() => {});
  eq(__jgetCalls, 5, "guard clears after error");
  __jgetDeferred.res({});
  await p7;
  console.log("PASS " + pass);
})().catch(e => { console.log("FAIL harness :: " + (e && e.stack || e)); process.exitCode = 1; });
''';


class L5WorkFetchDedupeTest(unittest.TestCase):
    def test_concurrent_open_shares_one_get(self):
        src = _script()
        script = (L5_STUBS + "\n" + _decl(src, "workFetchJoin")
                  + "\n" + L5_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L5 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_work_view_joins_instead_of_fetching(self):
        body = _fn_body(_script(), "renderWorkView")
        self.assertIn("await workFetchJoin(view.id)", body,
                      "work view must join the in-flight GET")
        self.assertNotIn('jget("/api/work?run="', body,
                         "work view must not fire its own work GET")


NAN_STUBS = r'''
var graphSel = null;
var gMode = "overview", ovNarrow = false;
var gapRows = [];
var GFONT = 12;
var GST_KO = {};
var gGroupSpine = null;
var narrowTree = false;
var narrowCol = false, laneSel = null;
var relHl = new Set();
function esc(s) { return String(s == null ? "" : s); }
function gLabel(n, len) { return String((n && (n.label || n.id)) || ""); }
function typeKo(t) { return String(t || ""); }
function graphColor(t) { return "#888888"; }
function edgeKo(k) { return String(k || ""); }
function showEdgeLabel(e) { return false; }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

NAN_CHECKS = r'''
// Reproduced demo set ("demo" search): the question node hangs off a
// run, with no repo-parent edge, so the overview mine/orphan split
// never positions it.
const R = {id: "repo:demo-sample-repo", type: "repo", label: "demo-sample-repo",
  status: "", _x: 60, _y: 46, _anchor: "start", _label: "demo-sample-repo"};
const W = {id: "run:run-demowait01", type: "run", label: "run-demowait01",
  status: "", _x: 316, _y: 20, _anchor: "start", _label: "run-demowait01"};
const Q = {id: "question:run-demowait01", type: "question", label: "question",
  status: "", _anchor: "start", _label: "question"};
// A) the real selector finds exactly the unpositioned neighbor.
const leftover = graphUnplaced([W, Q]);
eq(leftover.length, 1, "one leftover node");
eq(leftover[0].id, Q.id, "leftover is the repo-less neighbor");
// B) the bug mechanism: unpositioned input renders NaN through the
//    real node template (6 of the 8 live console errors).
const rawQ = __renderNode({id: "q0", type: "question", label: "q",
  _anchor: "start", _label: "q"});
ok(rawQ.indexOf("NaN") !== -1, "unpositioned node renders NaN (bug)");
// C) fixed flow: lay leftovers out, then render every node and edge
//    through the real templates -> no NaN/undefined anywhere.
leftover.forEach((nd, i) => { nd._x = 30 + (i % 1) * 250 + 16; nd._y = 400 + i * 62; });
const pos = {};
[R, W, Q].forEach(n => { pos[n.id] = n; });
let html = "";
[R, W, Q].forEach(n => { html += __renderNode(n); });
[{from: R.id, to: W.id, kind: "contains"},
 {from: W.id, to: Q.id, kind: "asks"}].forEach(e => { html += __renderEdge(e, pos); });
ok(html.indexOf("NaN") === -1, "no NaN in any rendered attribute");
ok(html.indexOf("undefined") === -1, "no undefined in any rendered attribute");
console.log("PASS " + pass);
''';


def _paint_fragments(src):
    """Real edge/node template fragments out of paintGraph."""
    paint = _fn_body(src, "paintGraph")
    e_open = "const a = pos[e.from], b = pos[e.to];"
    e_close = '}).join("") + nodes.map(n => {'
    assert paint.index(e_open) == paint.rindex(e_open), "edge anchor not unique"
    edge = paint[paint.index(e_open):paint.index(e_close)]
    n_open = "const fullName = n._fullLabel"
    n_close = "lineTags + `</g>`;"
    assert paint.index(n_open) == paint.rindex(n_open), "node anchor not unique"
    assert paint.index(n_close) == paint.rindex(n_close), "node end not unique"
    node = paint[paint.index(n_open):paint.index(n_close) + len(n_close)]
    return edge, node


class GraphNaNAttrsTest(unittest.TestCase):
    # Pre-existing since at least 243c: after a label search whose
    # neighbor set includes a node without a repo-parent edge in the
    # filtered set (demo: question:run-demowait01 hangs off a run),
    # the overview layout never positions it, and the templates emit
    # NaN/undefined position attributes (8 console errors).

    def test_leftover_layout_renders_clean_attrs(self):
        src = _script()
        edge, node = _paint_fragments(src)
        script = (NAN_STUBS + "\n" + _decl(src, "graphUnplaced")
                  + "\nfunction __renderEdge(e, pos) {\n" + edge + "\n}"
                  + "\nfunction __renderNode(n) {\n" + node + "\n}"
                  + "\n" + NAN_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"NaN failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_overview_branches_lay_out_leftovers(self):
        paint = _fn_body(_script(), "paintGraph")
        self.assertEqual(paint.count("graphUnplaced(rest)"), 2,
                         "both overview branches must lay out leftovers")
        self.assertIn("n._x = undefined; n._y = undefined;", paint,
                      "layout must clear stale positions first")
        self.assertNotIn("linked.add(e.to)", paint,
                         "edge-membership orphan guess is replaced")


ZERO_STUBS = r'''
var __gzero = false;
function $(id) {
  if (id === "gsvg") {
    return {getBoundingClientRect: function () {
      return {width: __gzero ? 0 : 316};
    }};
  }
  return null;
}
var document = {querySelector: function (sel) {
  if (sel !== ".glayout") return null;
  return {classList: {toggle: function (c, on) {
    if (c === "gzero") __gzero = !!on;
  }}};
}};
function fitGraphChrome() {}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

ZERO_CHECKS = r'''
// A previous zero paint left gzero on: at <=820px the canvas is
// display:none, so the svg measures 0 wide.
// A new paint with hits runs the two real calls in paintGraph's
// real order (CLEARED_FIRST is read from the shipped source).
__gzero = true;
if (CLEARED_FIRST) setGraphZero(false);
const measured = graphCanvasW(640);
if (!CLEARED_FIRST) setGraphZero(false);
eq(measured, 316, "paint with hits after zero measures the canvas");
ok(measured < 560, "narrow branch taken, not the 640 fallback");
console.log("PASS " + pass);
''';


class GraphZeroMeasureTest(unittest.TestCase):
    # 245c (introduced with 243c L2): at <=820px a zero paint hides
    # the canvas (display:none), and the next paint with hits measured
    # the canvas BEFORE clearing gzero — width 0, so the 640 fallback
    # ran the wide grid squeezed into ~316px (scale ~0.49, 23.7px hit
    # rects, ~6px labels). The zero state must clear before measuring;
    # zero branches set it back synchronously in the same pass.

    def _cleared_first(self):
        paint = _fn_body(_script(), "paintGraph")
        self.assertEqual(paint.count("graphCanvasW(640)"), 1,
                         "single canvas measure in paintGraph")
        return (paint.index("setGraphZero(false);")
                < paint.index("graphCanvasW(640)"))

    def test_zero_then_hits_measures_canvas(self):
        src = _script()
        script = (ZERO_STUBS + "\n" + _decl(src, "setGraphZero")
                  + "\n" + _decl(src, "graphCanvasW")
                  + "\nvar CLEARED_FIRST = "
                  + ("true" if self._cleared_first() else "false") + ";\n"
                  + ZERO_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"zero-measure failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_zero_cleared_before_measure(self):
        paint = _fn_body(_script(), "paintGraph")
        self.assertLess(paint.index("setGraphZero(false);"),
                        paint.index("graphCanvasW(640)"),
                        "zero state must clear before the canvas is measured")


class L1bSharedFetchTest(unittest.TestCase):
    def test_one_fetch_per_open_contract(self):
        # Behavior is pinned by the extended poll driver (10d3/10d4 in
        # tests/test_graph_poll_ui_logic.py); these pin the wiring.
        src = _script()
        view = _fn_body(src, "renderGraphView")
        self.assertIn("gGraphFetch && gGraphFetch.repo === reqRepo", view)
        self.assertIn("graphFetchDone(pp0);", view)
        poll = _fn_body(src, "graphPollRefresh")
        self.assertIn("if (gGraphFetch && gGraphFetch.repo === reqRepo)"
                      " return;", poll)
        self.assertIn("graphFetchDone(pp);", poll)
        self.assertNotIn("gPollInflight", src,
                         "the shared marker replaces the poll-only flag")


if __name__ == "__main__":
    unittest.main()
