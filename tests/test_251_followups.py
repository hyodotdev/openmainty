"""Gate-3 live-check followups (251/253): L10b narrow-tree rightward
leftover elbows; L10c/d on-demand relation lanes in narrow mode.

Real dashboard functions extracted and executed under node with
stub DOM; product constants extracted verbatim, never hand-mirrored.
"""

import json
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


L10B_FRAG_STUBS = r'''
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

L10B_FRAG_CHECKS = r'''
// Root's live coords: rightward root-column -> child-column edges
// across every branch row.
// L10c/d: narrow relations draw only for the selected node, so each
// case selects its own edge first (the lane routing is unchanged).
// Same-column straight applies to true row neighbours (dy = ROWH);
// longer same-column spans ride a lane (L10c through-node fix).
var pos = {
  left1: {id: "run:run-demowait01", type: "run", _x: 40, _y: 3052},
  kid1: {id: "issue:7", type: "issue", _x: 88, _y: 174},
  left2: {id: "run:run-demodone01", type: "run", _x: 40, _y: 3114},
  kid2: {id: "issue:3", type: "issue", _x: 88, _y: 236},
  col1: {id: "run:c1", type: "run", _x: 88, _y: 100},
  col2: {id: "run:c2", type: "run", _x: 88, _y: 162},
  colL: {id: "run:cL", type: "run", _x: 88, _y: 286},
  row1: {id: "run:r1", type: "run", _x: 40, _y: 100},
  row2: {id: "run:r2", type: "run", _x: 88, _y: 100}
};
var eLane1 = {from: "left1", to: "kid1", kind: "related", provenance: ""};
graphSel = "kid1"; relLanes.set(eLane1, 16);
var lane1 = __renderEdge(eLane1, pos);
ok(lane1.indexOf("<path") === 0, "rightward edge renders a path");
ok(lane1.indexOf("M 30 3052 H 16 V 174 H 78") !== -1,
  "lane rides x=16 :: " + lane1);
ok(lane1.indexOf("<line") === -1, "no diagonal for rightward edge");
var eLane2 = {from: "left2", to: "kid2", kind: "related", provenance: ""};
graphSel = "kid2"; relLanes.set(eLane2, 16);
var lane2 = __renderEdge(eLane2, pos);
ok(lane2.indexOf("M 30 3114 H 16 V 236 H 78") !== -1,
  "second lane rides x=16 :: " + lane2);
var eVert = {from: "col1", to: "col2", kind: "related", provenance: ""};
graphSel = "col1"; relLanes.set(eVert, 16);
var vert = __renderEdge(eVert, pos);
ok(vert.indexOf("<line") === 0, "adjacent same column keeps a straight line");
var eVertL = {from: "col1", to: "colL", kind: "related", provenance: ""};
graphSel = "col1"; relLanes.set(eVertL, 16);
var vertL = __renderEdge(eVertL, pos);
ok(vertL.indexOf("<path") === 0, "long same-column edge renders a path");
ok(vertL.indexOf("M 78 100 H 16 V 286 H 78") !== -1,
  "long same-column rides the lane :: " + vertL);
var eHoriz = {from: "row1", to: "row2", kind: "related", provenance: ""};
graphSel = "row1"; relLanes.set(eHoriz, 16);
var horiz = __renderEdge(eHoriz, pos);
ok(horiz.indexOf("<line") === 0, "same row keeps a straight line");
narrowTree = false; narrowCol = false;
var straight = __renderEdge(eLane1, pos);
ok(straight.indexOf("<line") === 0, "wide mode keeps straight lines");
console.log("PASS " + pass);
''';


class L10bRightwardElbowTest(unittest.TestCase):
    # Narrow-tree RIGHTWARD edges from a root-column leftover node
    # to a child-column node must ride the x=16 return channel like
    # L10 (direction-symmetric), instead of cutting diagonally
    # across every branch row. Adjacent same-column, same-row and
    # wide-mode edges keep their straight lines; longer same-column
    # spans ride a lane (L10c). Since L10c/d, narrow relations draw
    # only for the selected node, so each case selects its edge.

    def test_narrow_tree_routes_rightward_edges_through_lane(self):
        # Runs the REAL edge fragment (same anchors as the NaN and
        # L10 tests) with Root's live coords.
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
        script = (L10B_FRAG_STUBS + "\nconst nodeR = %s;" % m.group(1)
                  + "\n" + mrow.group(0)
                  + "\n" + _decl(src, "leftoverLaneD") + lane_d
                  + "\nfunction __renderEdge(e, pos) {\n" + edge + "\n}"
                  + "\n" + L10B_FRAG_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10b failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


L10CD_STUBS = r'''
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

L10CD_POS = r'''
// Live "issue" shape in miniature: two repos with pitched children
// (ROWH 62) plus root-column leftovers linked across columns, and a
// same-column pair spanning 10 rows (c0 -> c9, dy 638). Coords follow
// the narrow-tree layout pitch exactly (repo y+6, kids y+54+i*62).
var RPOS = {
  r1: {id: "repo:r1", type: "repo", _x: 40, _y: 46},
  c0: {id: "run:c0", type: "run", _x: 88, _y: 94},
  c1: {id: "issue:c1", type: "issue", _x: 88, _y: 156},
  c2: {id: "run:c2", type: "run", _x: 88, _y: 218},
  c3: {id: "run:c3", type: "run", _x: 88, _y: 280},
  c4: {id: "issue:c4", type: "issue", _x: 88, _y: 342},
  c5: {id: "run:c5", type: "run", _x: 88, _y: 404},
  r2: {id: "repo:r2", type: "repo", _x: 40, _y: 498},
  c6: {id: "run:c6", type: "run", _x: 88, _y: 546},
  c7: {id: "issue:c7", type: "issue", _x: 88, _y: 608},
  c8: {id: "run:c8", type: "run", _x: 88, _y: 670},
  c9: {id: "issue:c9", type: "issue", _x: 88, _y: 732},
  q1: {id: "branch:q1", type: "branch", _x: 40, _y: 820},
  q2: {id: "branch:q2", type: "branch", _x: 40, _y: 882}
};
// Indices 0-9 structural (repo -> child), 10-15 relations. c1's own
// relations are q1 -> c1 (rightward, index 12) and c1 -> c4
// (same-column long, index 13).
var REDGES = [
  {from: "r1", to: "c0", kind: "contains", provenance: ""},
  {from: "r1", to: "c1", kind: "contains", provenance: ""},
  {from: "r1", to: "c2", kind: "contains", provenance: ""},
  {from: "r1", to: "c3", kind: "contains", provenance: ""},
  {from: "r1", to: "c4", kind: "contains", provenance: ""},
  {from: "r1", to: "c5", kind: "contains", provenance: ""},
  {from: "r2", to: "c6", kind: "contains", provenance: ""},
  {from: "r2", to: "c7", kind: "contains", provenance: ""},
  {from: "r2", to: "c8", kind: "contains", provenance: ""},
  {from: "r2", to: "c9", kind: "contains", provenance: ""},
  {from: "c0", to: "c9", kind: "related", provenance: ""},
  {from: "c2", to: "c5", kind: "related", provenance: ""},
  {from: "q1", to: "c1", kind: "branch", provenance: ""},
  {from: "c1", to: "c4", kind: "related", provenance: ""},
  {from: "q2", to: "c8", kind: "branch", provenance: ""},
  {from: "c3", to: "q1", kind: "related", provenance: ""}
];
''';

L10CD_GEO = r'''
// Segment-vs-circle geometry: every drawn segment against every
// non-endpoint node circle (radius nodeR + 1px margin).
function segDist2(px, py, ax, ay, bx, by) {
  var dx = bx - ax, dy = by - ay;
  var L2 = dx * dx + dy * dy;
  var t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = Math.max(0, Math.min(1, t));
  var cx = ax + t * dx, cy = ay + t * dy;
  return (px - cx) * (px - cx) + (py - cy) * (py - cy);
}
function outSegs(out) {
  var m = out.match(/^<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"/);
  if (m) return [[+m[1], +m[2], +m[3], +m[4]]];
  m = out.match(/^<path d="M ([\d.]+) ([\d.]+) V ([\d.]+) H ([\d.]+)"/);
  if (m) return [[+m[1], +m[2], +m[1], +m[3]], [+m[1], +m[3], +m[4], +m[3]]];
  m = out.match(/^<path d="M ([\d.]+) ([\d.]+) H ([\d.]+) V ([\d.]+) H ([\d.]+)"/);
  if (m) {
    return [[+m[1], +m[2], +m[3], +m[2]], [+m[3], +m[2], +m[3], +m[4]],
            [+m[3], +m[4], +m[5], +m[4]]];
  }
  if (out === "") return [];
  return null;
}
''';

L10CD_CHECKS_A = r'''
// (a) narrow + no selection: structure only, relations hidden, the
// hint carries the exact hidden count.
graphSel = null;
relLanes = incidentLaneMap(REDGES, RPOS, graphSel, nodeR, true);
eq(relLanes.size, 0, "no selection -> no lanes");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var OUTS = REDGES.map(e => __renderEdge(e, RPOS));
eq(OUTS.filter(o => o === "").length, 6, "6 relations hidden");
eq(OUTS.filter(o => o.indexOf("<path") === 0).length, 10,
  "10 structural elbows drawn");
for (var i = 10; i < 16; i++) eq(OUTS[i], "", "relation hidden :: " + i);
eq(hiddenRels, 6, "hidden counter exact");
eq(drawnRels, 0, "drawn counter zero");
eq(relationHintText(hiddenRels, drawnRels),
  "관계선 6개 숨김 — 노드를 선택하면 그 노드의 관계만 표시합니다",
  "hint text exact");
console.log("PASS " + pass);
''';

L10CD_CHECKS_B = r'''
// (b) narrow + selection: exactly c1's relations ride distinct
// lanes; nothing crosses a non-endpoint circle.
graphSel = "c1";
relLanes = incidentLaneMap(REDGES, RPOS, graphSel, nodeR, true);
eq(relLanes.size, 2, "only c1's relations get lanes");
ok(!relLanes.has(REDGES[1]), "structural incident edge takes no lane slot");
var lxs = [];
relLanes.forEach(lx => lxs.push(lx));
eq(new Set(lxs).size, 2, "lanes distinct :: " + JSON.stringify(lxs));
eq(lxs[0], 16, "first lane x=16");
eq(lxs[1], 22, "second lane x=22");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var OUTS = REDGES.map(e => __renderEdge(e, RPOS));
eq(OUTS.filter(o => o === "").length, 4, "non-incident relations hidden");
[10, 11, 14, 15].forEach(i => eq(OUTS[i], "", "hidden :: " + i));
[12, 13].forEach(i => {
  ok(OUTS[i].indexOf("<path") === 0, "incident relation draws a path :: " + i);
  ok(/H [\d.]+ V/.test(OUTS[i]), "incident relation rides a lane :: " + OUTS[i]);
  ok(OUTS[i].indexOf('stroke="#8fb4e8"') !== -1, "hot stroke :: " + i);
});
eq(hiddenRels, 4, "hidden counter");
eq(drawnRels, 2, "drawn counter");
ok(relHl.has("q1") && relHl.has("c1") && relHl.has("c4"),
  "endpoints collected");
eq(relHl.size, 3, "exactly the incident endpoints");
var violations = [];
OUTS.forEach((out, i) => {
  var segs = outSegs(out);
  ok(segs !== null, "output parses :: " + out.slice(0, 60));
  if (!segs) return;
  var ends = {};
  ends[REDGES[i].from] = 1; ends[REDGES[i].to] = 1;
  segs.forEach(s => {
    Object.keys(RPOS).forEach(k => {
      if (ends[k]) return;
      var n = RPOS[k];
      var rr = nodeR(n) + 1;
      if (segDist2(n._x, n._y, s[0], s[1], s[2], s[3]) <= rr * rr) {
        violations.push([i, k, s]);
      }
    });
  });
});
eq(violations.length, 0,
  "0 through-node intersections :: " + JSON.stringify(violations.slice(0, 2)));
eq(relationHintText(hiddenRels, drawnRels),
  "관계선 4개 숨김 — 선택한 노드의 관계 2개 표시 중", "hint text exact");
console.log("PASS " + pass);
''';

L10CD_CHECKS_D = r'''
// (d) the hint count follows the filtered set, and the live region
// is rewritten only when the text changes.
graphSel = "c1";
relLanes = incidentLaneMap(REDGES, RPOS, graphSel, nodeR, true);
hiddenRels = 0; drawnRels = 0; relHl = new Set();
REDGES.forEach(e => __renderEdge(e, RPOS));
var hint1 = relationHintText(hiddenRels, drawnRels);
var FEWER = REDGES.filter(e => e !== REDGES[10] && e !== REDGES[11]);
relLanes = incidentLaneMap(FEWER, RPOS, graphSel, nodeR, true);
hiddenRels = 0; drawnRels = 0; relHl = new Set();
FEWER.forEach(e => __renderEdge(e, RPOS));
var hint2 = relationHintText(hiddenRels, drawnRels);
eq(hint1, "관계선 4개 숨김 — 선택한 노드의 관계 2개 표시 중",
  "hint before filter");
eq(hint2, "관계선 2개 숨김 — 선택한 노드의 관계 2개 표시 중",
  "hint after filter");
ok(hint1 !== hint2, "hint updates when the set changes");
var writes = [], toggles = [];
var fakeEl = {};
Object.defineProperty(fakeEl, "textContent", {
  set: function(v) { writes.push(v); },
  get: function() { return ""; }
});
fakeEl.classList = {toggle: function(c, f) { toggles.push([c, f]); }};
__hintEl = fakeEl;
setRelationHint("a"); setRelationHint("a");
setRelationHint("b"); setRelationHint("");
eq(JSON.stringify(writes), JSON.stringify(["a", "b", ""]),
  "live region written only on change");
eq(JSON.stringify(toggles),
  JSON.stringify([["hidden", false], ["hidden", false], ["hidden", true]]),
  "hint hides exactly when empty");
console.log("PASS " + pass);
''';

L10CD_HINT_STUBS = r'''
var __hintEl = null;
var gRelHintMsg = null, gRelHintEl = null;
function $(id) { return id === "grelhint" ? __hintEl : null; }
''';

# (c) wide-mode golden: captured from the pre-change product by the
# /tmp throwaway (muse253-capture-wide-golden.py), then frozen. Wide
# output must stay byte-identical.
L10CD_WIDE_GOLDEN = [
    '<line x1="40" y1="46" x2="88" y2="94" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="46" x2="88" y2="156" stroke="#8fb4e8" stroke-width="2"><title>contains · </title></line>',
    '<line x1="40" y1="46" x2="88" y2="218" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="46" x2="88" y2="280" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="46" x2="88" y2="342" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="46" x2="88" y2="404" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="498" x2="88" y2="546" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="498" x2="88" y2="608" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="498" x2="88" y2="670" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="40" y1="498" x2="88" y2="732" stroke="#88888855" stroke-width="1"><title>contains · </title></line>',
    '<line x1="88" y1="94" x2="88" y2="732" stroke="#88888855" stroke-width="1"><title>related · </title></line>',
    '<line x1="88" y1="218" x2="88" y2="404" stroke="#88888855" stroke-width="1"><title>related · </title></line>',
    '<line x1="40" y1="820" x2="88" y2="156" stroke="#8fb4e8" stroke-width="2"><title>branch · </title></line>',
    '<line x1="88" y1="156" x2="88" y2="342" stroke="#8fb4e8" stroke-width="2"><title>related · </title></line>',
    '<line x1="40" y1="882" x2="88" y2="670" stroke="#88888855" stroke-width="1"><title>branch · </title></line>',
    '<line x1="88" y1="280" x2="40" y2="820" stroke="#88888855" stroke-width="1"><title>related · </title></line>',
]


def _l10cd_script(checks, extra_stubs="", extra_decls=()):
    src = _script()
    paint = _fn_body(src, "paintGraph")
    e_open = "const a = pos[e.from], b = pos[e.to];"
    e_close = '}).join("") + nodes.map(n => {'
    assert paint.count(e_open) == 1, "edge anchor not unique"
    edge = paint[paint.index(e_open):paint.index(e_close)]
    m = re.search(r"const nodeR = (n => [^;]+);", paint)
    assert m, "nodeR radius rule missing"
    mrow = re.search(r"const COLW = \d+, ROWH = \d+;", paint)
    assert mrow, "ROWH layout pitch missing"
    parts = [L10CD_STUBS + extra_stubs,
             "const nodeR = %s;" % m.group(1), mrow.group(0),
             _decl(src, "leftoverLaneD")]
    for name in ("incidentLaneMap", "relationLaneX", "relationLaneD",
                 "relationHintText") + tuple(extra_decls):
        parts.append(_decl(src, name))
    parts.append(L10CD_GEO)
    parts.append("function __renderEdge(e, pos) {\n" + edge + "\n}")
    parts.append(L10CD_POS)
    parts.append(checks)
    return "\n".join(parts)


def _l10cd_wide_outputs():
    """Render the L10c/d fixture through the REAL edge fragment in
    wide mode (narrowTree=false, c1 selected). Used by the (c) golden
    test and by the pre-change golden-capture throwaway."""
    src = _script()
    paint = _fn_body(src, "paintGraph")
    e_open = "const a = pos[e.from], b = pos[e.to];"
    e_close = '}).join("") + nodes.map(n => {'
    assert paint.count(e_open) == 1, "edge anchor not unique"
    edge = paint[paint.index(e_open):paint.index(e_close)]
    m = re.search(r"const nodeR = (n => [^;]+);", paint)
    assert m, "nodeR radius rule missing"
    mrow = re.search(r"const COLW = \d+, ROWH = \d+;", paint)
    assert mrow, "ROWH layout pitch missing"
    lane_d = (("\n" + _decl(src, "relationLaneD"))
              if "function relationLaneD(" in src else "")
    script = (L10CD_STUBS + "\nconst nodeR = %s;" % m.group(1)
              + "\n" + mrow.group(0)
              + "\n" + _decl(src, "leftoverLaneD") + lane_d
              + "\nfunction __renderEdge(e, pos) {\n" + edge + "\n}"
              + "\n" + L10CD_POS
              + '\ngraphSel = "c1"; narrowTree = false; narrowCol = false;'
                "\nvar OUTS = REDGES.map(e => __renderEdge(e, RPOS));"
                "\nconsole.log(JSON.stringify(OUTS));")
    proc = _run_node(script)
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class L10cdHiddenByDefaultTest(unittest.TestCase):
    # (a) narrow + no selection: only the structural tree is drawn
    # and the hint shows the exact hidden relation count.

    def test_narrow_without_selection_hides_relations(self):
        src = _script()
        for name in ("incidentLaneMap", "relationLaneD",
                     "relationHintText"):
            self.assertIn("function %s(" % name, src,
                          "missing L10c/d helper " + name)
        proc = _run_node(_l10cd_script(L10CD_CHECKS_A))
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10cd(a) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10cdSelectedLanesTest(unittest.TestCase):
    # (b) narrow + selection: exactly that node's relations ride
    # distinct lanes; no drawn segment crosses a non-endpoint circle.

    def test_selected_node_relations_use_distinct_clean_lanes(self):
        src = _script()
        for name in ("incidentLaneMap", "relationLaneX",
                     "relationLaneD", "relationHintText"):
            self.assertIn("function %s(" % name, src,
                          "missing L10c/d helper " + name)
        paint = _fn_body(src, "paintGraph")
        self.assertIn("incidentLaneMap(edges, pos, laneSel, nodeR,"
                      " narrowTree || gMode === \"group\")",
                      paint, "lane table must be wired to the selection")
        self.assertIn("relHl.has(n.id)", paint,
                      "node template must read the endpoint set")
        head = HTML.read_text().split("<script>")[0]
        self.assertIn("#gsvg .nd.rel circle", head,
                      "missing endpoint-ring CSS rule")
        proc = _run_node(_l10cd_script(L10CD_CHECKS_B))
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10cd(b) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10cdWideUnchangedTest(unittest.TestCase):
    # (c) wide mode renders byte-identical edge output before/after.

    def test_wide_mode_edge_output_byte_identical(self):
        self.assertTrue(L10CD_WIDE_GOLDEN, "wide golden not captured")
        self.assertEqual(_l10cd_wide_outputs(), L10CD_WIDE_GOLDEN)


class L10cdHintUpdatesTest(unittest.TestCase):
    # (d) the hint text/count follow filters and search, and the
    # polite live region is rewritten only when the text changes.

    def test_hint_follows_filtered_set_and_writes_on_change(self):
        src = _script()
        for name in ("incidentLaneMap", "relationHintText",
                     "setRelationHint"):
            self.assertIn("function %s(" % name, src,
                          "missing L10c/d helper " + name)
        self.assertIn('id="grelhint"', src, "missing hint element")
        proc = _run_node(_l10cd_script(
            L10CD_CHECKS_D, extra_stubs=L10CD_HINT_STUBS,
            extra_decls=("setRelationHint",)))
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10cd(d) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


if __name__ == "__main__":
    unittest.main()
