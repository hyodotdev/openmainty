"""Gate-3 live-check followups (249): L13 pager focus, P1 cold
graph load, L8 narrow-tree hit overlap, L10 narrow elbows.

Real dashboard functions extracted and executed under node with
stub DOM; product constants extracted verbatim, never hand-mirrored.
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


def _balanced(src, i):
    """Balanced-brace body starting at src[i] == '{'."""
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
    raise AssertionError("unbalanced braces")


def _fn_body(src, name):
    """Balanced-brace body of a top-level (async) function."""
    m = re.search(r"(?:^|\n)(?:async\s+)?function\s+" + re.escape(name)
                  + r"\s*\(", src)
    assert m, f"missing function {name}"
    return _balanced(src, src.index("{", m.start()))


def _block_body(src, marker):
    """Balanced-brace body of the block whose '{' follows marker."""
    assert src.count(marker) == 1, f"block marker not unique: {marker}"
    return _balanced(src, src.index("{", src.index(marker)))


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


L13_STUBS = r'''
var gSearchPage = 0, gSearchPages = 1, gNeedFit = false;
var __active = "BODY";
function mkBtn(id) {
  let dis = false;
  return {
    id: id,
    classList: {toggle: function () {}},
    focus: function (o) { __active = id; },
    get disabled() { return dis; },
    // Real browsers drop focus to BODY the moment the focused
    // button becomes disabled; the stub models exactly that.
    set disabled(v) {
      dis = !!v;
      if (dis && __active === id) __active = "BODY";
    }
  };
}
var __btns = {prev: mkBtn("prev"), next: mkBtn("next")};
var __body = {tagName: "BODY"};
function $(id) {
  if (id === "g-prevpg") return __btns.prev;
  if (id === "g-nextpg") return __btns.next;
  return null;
}
var document = {get activeElement() {
  return __active === "BODY" ? __body : __btns[__active];
}};
// Mirrors the real paintGraph write-back (graphSearchPage clamps
// gSearchPage to the last page) before the real pager paint.
function paintGraph() {
  if (gSearchPage > gSearchPages - 1) gSearchPage = gSearchPages - 1;
  if (gSearchPage < 0) gSearchPage = 0;
  paintSearchPager();
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L13_CHECKS = r'''
// 9 pages of results; Enter-repeat on "next" to the last page.
gSearchPages = 9; gSearchPage = 0;
paintSearchPager();
__active = "next";
for (let i = 0; i < 20; i++) graphPagerNext();
eq(gSearchPage, 8, "Enter-repeat reaches the last page");
ok(__active !== "BODY", "focus never drops to BODY going forward");
eq(__active, "prev", "disabled next hands focus to prev");
// Back to the first page the same way.
for (let i = 0; i < 20; i++) graphPagerPrev();
eq(gSearchPage, 0, "reaches the first page");
ok(__active !== "BODY", "focus never drops to BODY going back");
eq(__active, "next", "disabled prev hands focus to next");
// Mid-list paging keeps focus where it is.
__active = "next"; gSearchPage = 3;
graphPagerNext();
eq(gSearchPage, 4, "mid-list advance works");
eq(__active, "next", "focus stays on an enabled button");
console.log("PASS " + pass);
''';


class L13PagerFocusTest(unittest.TestCase):
    # When the focused pager button becomes disabled (last/first
    # page), browsers drop focus to BODY. The pager must hand focus
    # to the still-enabled sibling instead.

    def test_pager_keeps_keyboard_place(self):
        src = _script()
        script = (L13_STUBS + "\n" + _decl(src, "paintSearchPager")
                  + "\n" + _decl(src, "graphPagerPrev")
                  + "\n" + _decl(src, "graphPagerNext")
                  + "\n" + L13_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L13 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_pager_buttons_use_named_handlers(self):
        view = _fn_body(_script(), "renderGraphView")
        self.assertIn("gpp.onclick = graphPagerPrev", view,
                      "prev button uses the named handler")
        self.assertIn("gnp.onclick = graphPagerNext", view,
                      "next button uses the named handler")


L8_STUBS = r'''
var nodes = [
  {id: "repo:r1", type: "repo"},
  {id: "run:a", type: "run"},
  {id: "run:b", type: "run"},
  {id: "issue:c", type: "issue"},
  {id: "repo:r2", type: "repo"},
  {id: "run:d", type: "run"},
  {id: "question:q", type: "question"},
  {id: "run:orphan", type: "run"}
];
var edges = [
  {from: "repo:r1", to: "run:a"},
  {from: "repo:r1", to: "run:b"},
  {from: "repo:r1", to: "issue:c"},
  {from: "repo:r2", to: "run:d"},
  {from: "run:a", to: "question:q"}
];
var repos = nodes.filter(n => n.type === "repo");
var rest = nodes.filter(n => n.type !== "repo");
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L8_CHECKS = r'''
nodes.forEach(function (n) {
  ok(Number.isFinite(n._x) && Number.isFinite(n._y), "placed " + n.id);
});
function rrect(n) {
  return {x0: n._x - RO, x1: n._x - RO + RS,
          y0: n._y - RO, y1: n._y - RO + RS};
}
for (var i = 0; i < nodes.length; i++) {
  for (var j = i + 1; j < nodes.length; j++) {
    var A = rrect(nodes[i]), B = rrect(nodes[j]);
    if (A.x0 < B.x1 && B.x0 < A.x1 && A.y0 < B.y1 && B.y0 < A.y1) {
      console.log("FAIL overlap " + nodes[i].id + " vs " + nodes[j].id +
        " :: [" + A.x0 + "," + A.y0 + "," + A.x1 + "," + A.y1 + "] vs [" +
        B.x0 + "," + B.y0 + "," + B.x1 + "," + B.y1 + "]");
      process.exitCode = 1;
    } else { pass++; }
  }
}
console.log("PASS " + pass);
''';


class L8NarrowTreeOverlapTest(unittest.TestCase):
    # The narrow-overview indent tree (ROOT_X/CHILD_X) must place
    # every node, and the invisible 48px hit rects (verbatim
    # geometry) must never intersect: siblings ride the 62px pitch,
    # and the root-to-first-child step clears 48px on both axes.
    # The driver runs the REAL layout block with a synthetic tree.

    def test_narrow_tree_hit_rects_do_not_overlap(self):
        src = _script()
        block = _block_body(src, "} else if (ovNarrow) {")
        m = re.search(r"const COLW = (\d+), ROWH = (\d+);", src)
        self.assertIsNotNone(m, "ROWH pitch missing from dashboard")
        r = re.search(r'<rect x="\$\{n\._x - (\d+)\}" '
                      r'y="\$\{n\._y - (\d+)\}" '
                      r'width="(\d+)" height="(\d+)"', src)
        self.assertIsNotNone(r, "hit rect template missing")
        roff, w, h = int(r.group(1)), int(r.group(3)), int(r.group(4))
        self.assertEqual(int(r.group(2)), roff, "rect must be square-centred")
        self.assertEqual(w, h, "hit rect must be square")
        self.assertGreaterEqual(w, 44, "hit rect must stay >=44px")
        script = (L8_STUBS + "\nconst ROWH = %s;" % m.group(2)
                  + "\n" + _decl(src, "graphUnplaced")
                  + "\n" + block[1:-1]
                  + "\nvar RS = %d, RO = %d;" % (w, roff)
                  + "\n" + L8_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L8 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


L10_STUBS = r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
''';

L10_CHECKS = r'''
eq(leftoverLaneD(62, 100, 30, 200), "M 62 100 H 16 V 200 H 30",
  "downward lane");
eq(leftoverLaneD(62, 200, 30, 100), "M 62 200 H 16 V 100 H 30",
  "upward lane");
console.log("PASS " + pass);
''';

L10_FRAG_STUBS = r'''
var graphSel = null;
var gGroupSpine = null;
var narrowTree = true;
var narrowCol = true;
var gMode = "overview", ovNarrow = true;
var gapRows = [];
var relLanes = new Map();
var hiddenRels = 0, drawnRels = 0;
var relHl = new Set();
function esc(s) { return String(s == null ? "" : s); }
function edgeKo(k) { return String(k || ""); }
function showEdgeLabel(e) { return false; }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L10_FRAG_CHECKS = r'''
var pos = {
  child: {id: "run:a", type: "run", _x: 88, _y: 100},
  left: {id: "question:q", type: "question", _x: 40, _y: 200},
  repo: {id: "repo:r1", type: "repo", _x: 40, _y: 46},
  kid: {id: "run:b", type: "run", _x: 88, _y: 174}
};
// L10c/d: narrow relations draw only for the selected node, so the
// lane case selects its edge first (the routing itself is unchanged).
// The structural spine draws regardless of selection.
var eLane = {from: "child", to: "left", kind: "asks", provenance: ""};
graphSel = "child"; relLanes.set(eLane, 16);
var lane = __renderEdge(eLane, pos);
ok(lane.indexOf("<path") === 0, "leftward edge renders a path");
ok(lane.indexOf("M 78 100 H 16 V 200 H 30") !== -1,
  "lane rides x=16 :: " + lane);
ok(lane.indexOf("<line") === -1, "no diagonal for leftward edge");
var eSpine = {from: "repo", to: "kid", kind: "contains", provenance: ""};
var spine = __renderEdge(eSpine, pos);
ok(spine.indexOf("M 40 62 V 174 H 78") !== -1,
  "spine elbow intact :: " + spine);
narrowTree = false; narrowCol = false;
var straight = __renderEdge(eLane, pos);
ok(straight.indexOf("<line") === 0, "wide mode keeps straight lines");
console.log("PASS " + pass);
''';


class L10LeftoverElbowTest(unittest.TestCase):
    # Narrow-tree edges that bend back into the root column (to
    # leftover nodes) must ride the left return channel (x=16)
    # instead of cutting diagonally across the spine.

    def test_lane_path_is_pure_elbow(self):
        src = _script()
        self.assertIn("function leftoverLaneD(", src,
                      "missing left-lane path helper")
        script = L10_STUBS + "\n" + _decl(src, "leftoverLaneD") \
            + "\n" + _decl(src, "relationLaneD") \
            + "\n" + L10_CHECKS
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_narrow_tree_routes_leftward_edges_through_lane(self):
        # Runs the REAL edge fragment (same anchors as the NaN test)
        # with a leftward child->root-column edge: narrowTree emits
        # the x=16 lane, wide mode keeps the straight line, and the
        # repo spine elbow is unchanged. Since L10c/d the lane case
        # selects its edge (narrow relations need a selection).
        src = _script()
        paint = _fn_body(src, "paintGraph")
        e_open = "const a = pos[e.from], b = pos[e.to];"
        e_close = '}).join("") + nodes.map(n => {'
        self.assertEqual(paint.count(e_open), 1, "edge anchor not unique")
        edge = paint[paint.index(e_open):paint.index(e_close)]
        m = re.search(r"const nodeR = (n => [^;]+);", paint)
        self.assertIsNotNone(m, "nodeR radius rule missing")
        mrow = re.search(r"const COLW = \d+, ROWH = \d+;", paint)
        self.assertIsNotNone(mrow, "ROWH layout pitch missing")
        lane_d = (("\n" + _decl(src, "relationLaneD"))
                  if "function relationLaneD(" in src else "")
        script = (L10_FRAG_STUBS + "\nconst nodeR = %s;" % m.group(1)
                  + "\n" + mrow.group(0)
                  + "\n" + _decl(src, "leftoverLaneD") + lane_d
                  + "\nfunction __renderEdge(e, pos) {\n" + edge + "\n}"
                  + "\n" + L10_FRAG_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


if __name__ == "__main__":
    unittest.main()
