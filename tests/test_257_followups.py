"""Gate-3 live-check followups (257): L10f-wide gap/gutter relation lanes.

Wide (and "all") column/grid layouts draw every relation as a straight
line through unrelated nodes. This file pins the L10f-wide rule with
real dashboard functions under node: every column/grid layout at any
width gates relations (the 253/255 rule: structure plus the focused
node's relations, selection narrows, honest hint with exact counts)
and routes each drawn relation as an orthogonal path through row gaps
plus the left gutter, with zero through-node intersections and no
diagonals. The overview tree stays as-is, and an in-graph hash change
syncs the search box without stealing focus. Product constants
extracted verbatim, never hand-mirrored.
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


L10F_FRAG_STUBS = r'''
var graphSel = null, gFocus = null;
var gGroupSpine = null;
var narrowTree = false, narrowCol = false, laneSel = null;
var gMode = "overview", ovNarrow = false;
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

L10F_GEO = r'''
function segDist2(px, py, ax, ay, bx, by) {
  var dx = bx - ax, dy = by - ay;
  var L2 = dx * dx + dy * dy;
  var t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = Math.max(0, Math.min(1, t));
  var cx = ax + t * dx, cy = ay + t * dy;
  return (px - cx) * (px - cx) + (py - cy) * (py - cy);
}
// Orthogonal path tokenizer: M x y (H x | V y)*. Any other command
// (L, C, Q, T) or NaN coordinate is unparsed by design, so a
// diagonal or curve can never slip through as clean.
function outSegs(out) {
  var m = out.match(/^<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"/);
  if (m) return [[+m[1], +m[2], +m[3], +m[4]]];
  m = out.match(/^<path d="([^"]+)"/);
  if (m) {
    var toks = m[1].trim().split(/\s+/);
    if (toks[0] !== "M") return null;
    var x = +toks[1], y = +toks[2], i = 3, segs = [];
    if (!isFinite(x) || !isFinite(y)) return null;
    while (i < toks.length) {
      var c = toks[i];
      if (c === "H") {
        var nx = +toks[i + 1];
        if (!isFinite(nx)) return null;
        segs.push([x, y, nx, y]); x = nx; i += 2;
      } else if (c === "V") {
        var ny = +toks[i + 1];
        if (!isFinite(ny)) return null;
        segs.push([x, y, x, ny]); y = ny; i += 2;
      } else return null;
    }
    return segs;
  }
  if (out === "") return [];
  return null;
}
function throughNode(outs, edges, pos) {
  var bad = [];
  outs.forEach((out, i) => {
    var segs = outSegs(out);
    if (segs === null) { bad.push(["unparsed", i, out.slice(0, 60)]); return; }
    var ends = {};
    ends[edges[i].from] = 1; ends[edges[i].to] = 1;
    segs.forEach(s => {
      Object.keys(pos).forEach(k => {
        if (ends[k]) return;
        var n = pos[k];
        var rr = nodeR(n) + 1;
        if (segDist2(n._x, n._y, s[0], s[1], s[2], s[3]) <= rr * rr) {
          bad.push([i, k, s]);
        }
      });
    });
  });
  return bad;
}
// Straight lines must be axis-aligned (adjacent same-column rows).
function diagLines(outs) {
  return outs.filter(o => {
    var m = o.match(/^<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"/);
    return m && +m[1] !== +m[3] && +m[2] !== +m[4];
  });
}
''';

L10F_SEARCH_POS = r'''
// Wide search-list shape: the no-repos grid (x0=28 -> x=44/294,
// y0=40, ROWH pitch), 2 columns x 4 rows.
var APOS = {
  A0: {id: "issue:A0", type: "issue", _x: 44, _y: 40},
  A1: {id: "run:A1", type: "run", _x: 44, _y: 102},
  A2: {id: "run:A2", type: "run", _x: 44, _y: 164},
  A3: {id: "branch:A3", type: "branch", _x: 44, _y: 226},
  B0: {id: "issue:B0", type: "issue", _x: 294, _y: 40},
  B1: {id: "run:B1", type: "run", _x: 294, _y: 102},
  B2: {id: "run:B2", type: "run", _x: 294, _y: 164},
  B3: {id: "branch:B3", type: "branch", _x: 294, _y: 226}
};
// Indices 0,1,3,7 touch A0; 2,4,5,6 do not.
var AEDGES = [
  {from: "A0", to: "A3", kind: "related", provenance: ""},
  {from: "A0", to: "A1", kind: "related", provenance: ""},
  {from: "A1", to: "B1", kind: "related", provenance: ""},
  {from: "A0", to: "B2", kind: "related", provenance: ""},
  {from: "B0", to: "A3", kind: "related", provenance: ""},
  {from: "B1", to: "B2", kind: "related", provenance: ""},
  {from: "A2", to: "B3", kind: "related", provenance: ""},
  {from: "A0", to: "B1", kind: "related", provenance: ""}
];
''';

L10F_HUB_POS = r'''
// Wide focus-hub shape (the QA hub): center above the grid, 25
// neighbors in 2 columns x 13 rows, plus 2 neighbor-neighbor edges.
var HPOS = {C: {id: "issue:C", type: "issue", _x: 307, _y: 46}};
for (var hi = 0; hi < 25; hi++) {
  HPOS["N" + hi] = {id: "run:N" + hi, type: "run",
    _x: hi % 2 ? 296 : 46, _y: 130 + Math.floor(hi / 2) * 62};
}
var HEDGES = [];
for (var hj = 0; hj < 25; hj++) {
  HEDGES.push({from: "C", to: "N" + hj, kind: "about", provenance: ""});
}
HEDGES.push({from: "N0", to: "N5", kind: "related", provenance: ""});
HEDGES.push({from: "N3", to: "N4", kind: "related", provenance: ""});
''';

L10F_ALLW_POS = r'''
// Mode=all wide shape: 2 columns x 3 rows of mixed types.
var WPOS2 = {
  A0: {id: "issue:A0", type: "issue", _x: 46, _y: 40},
  A1: {id: "run:A1", type: "run", _x: 46, _y: 102},
  A2: {id: "run:A2", type: "run", _x: 46, _y: 164},
  B0: {id: "issue:B0", type: "issue", _x: 296, _y: 40},
  B1: {id: "branch:B1", type: "branch", _x: 296, _y: 102},
  B2: {id: "run:B2", type: "run", _x: 296, _y: 164}
};
// Indices 0,1 touch A0; 2,3 do not.
var WEDGES2 = [
  {from: "A0", to: "A2", kind: "related", provenance: ""},
  {from: "A0", to: "B1", kind: "related", provenance: ""},
  {from: "B0", to: "B2", kind: "related", provenance: ""},
  {from: "A1", to: "B1", kind: "related", provenance: ""}
];
''';

L10F_ALLN_POS = r'''
// Mode=all narrow shape: single column, 5 rows.
var NPOS = {
  N0: {id: "issue:N0", type: "issue", _x: 46, _y: 40},
  N1: {id: "run:N1", type: "run", _x: 46, _y: 102},
  N2: {id: "run:N2", type: "run", _x: 46, _y: 164},
  N3: {id: "branch:N3", type: "branch", _x: 46, _y: 226},
  N4: {id: "run:N4", type: "run", _x: 46, _y: 288}
};
// Indices 0,2 touch N0; 1,3 do not.
var NEDGES = [
  {from: "N0", to: "N4", kind: "related", provenance: ""},
  {from: "N1", to: "N2", kind: "related", provenance: ""},
  {from: "N0", to: "N2", kind: "related", provenance: ""},
  {from: "N3", to: "N4", kind: "related", provenance: ""}
];
''';

L10F_GROUP_POS = r'''
// Group shape: one repo above four members (gy0 = 46+62+20 = 128,
// groupRowH single-line pitch 62).
var GPOS = {
  R: {id: "repo:R", type: "repo", _x: 60, _y: 46},
  M0: {id: "run:M0", type: "run", _x: 92, _y: 128},
  M1: {id: "run:M1", type: "run", _x: 92, _y: 190},
  M2: {id: "run:M2", type: "run", _x: 92, _y: 252},
  M3: {id: "run:M3", type: "run", _x: 92, _y: 314}
};
var GEDGES = [
  {from: "R", to: "M0", kind: "contains", provenance: ""},
  {from: "R", to: "M1", kind: "contains", provenance: ""},
  {from: "M0", to: "M2", kind: "related", provenance: ""},
  {from: "M1", to: "M3", kind: "related", provenance: ""}
];
''';

L10F_NARROW_TREE_POS = r'''
// Narrow overview-tree shape (253): repo root, two children, one
// root-column leftover linked to a child.
var TPOS = {
  R: {id: "repo:R", type: "repo", _x: 40, _y: 46},
  K0: {id: "run:K0", type: "run", _x: 88, _y: 94},
  K1: {id: "run:K1", type: "run", _x: 88, _y: 156},
  L: {id: "issue:L", type: "issue", _x: 40, _y: 218}
};
var TEDGES = [
  {from: "R", to: "K0", kind: "contains", provenance: ""},
  {from: "R", to: "K1", kind: "contains", provenance: ""},
  {from: "K0", to: "K1", kind: "related", provenance: ""},
  {from: "L", to: "K1", kind: "works-on", provenance: ""}
];
''';

L10F_NARROW_FOCUS_POS = r'''
// Narrow focus shape (255): center above a single column.
var QPOS = {
  F: {id: "issue:F", type: "issue", _x: 163, _y: 46},
  N0: {id: "run:N0", type: "run", _x: 46, _y: 130},
  N1: {id: "run:N1", type: "run", _x: 46, _y: 192},
  N2: {id: "branch:N2", type: "branch", _x: 46, _y: 254}
};
var QEDGES2 = [
  {from: "F", to: "N0", kind: "about", provenance: ""},
  {from: "F", to: "N1", kind: "about", provenance: ""},
  {from: "N0", to: "N2", kind: "related", provenance: ""}
];
''';

L10F_CHECKS_A = r'''
// (a) wide search list: ungated straight lines cross the grid;
// gated, only the selection's relations draw, orthogonally, clean.
gMode = "overview"; ovNarrow = false; narrowTree = false;
gapRows = laneGapRows(Object.values(APOS), nodeR);
eq(gapRows.length, 4, "four grid rows");
// The before-state: 8 straight lines, frozen through-count.
narrowCol = false;
var AUNG = AEDGES.map(e => __renderEdge(e, APOS));
ok(AUNG.every(o => o.indexOf("<line") === 0), "ungated draws straight");
var AUNGBAD = throughNode(AUNG, AEDGES, APOS);
eq(AUNGBAD.length, L10F_A_UNGATED_THROUGH, "frozen before-count");
ok(diagLines(AUNG).length > 0, "ungated has diagonals");
// Gated: the scope admits overview+query at wide widths.
var scA = laneScope(false, false, "overview", false, "A0", null, true);
eq(scA.col, true, "overview+query gates at wide widths");
eq(scA.sel, "A0", "selection is the scope");
narrowCol = scA.col; laneSel = scA.sel;
relLanes = incidentLaneMap(AEDGES, APOS, laneSel, nodeR, false);
eq(relLanes.size, 4, "the 4 A0 relations get lanes");
var alx = [];
relLanes.forEach(lx => alx.push(lx));
eq(new Set(alx).size, 4, "lanes distinct");
eq(JSON.stringify(alx), JSON.stringify([16, 19, 22, 25]),
  "lanes pack the gutter");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var AOUTS = AEDGES.map(e => __renderEdge(e, APOS));
eq(AOUTS.filter(o => o === "").length, 4, "non-A0 relations hidden");
[2, 4, 5, 6].forEach(i => eq(AOUTS[i], "", "hidden :: " + i));
eq(hiddenRels, 4, "hidden counter");
eq(drawnRels, 4, "drawn counter");
eq(relationHintText(hiddenRels, drawnRels, "선택한 노드"),
  "관계선 4개 숨김 — 선택한 노드의 관계 4개 표시 중", "search hint text");
// Shapes: same-column span -> 5 segments, adjacent rows -> one
// straight vertical, cross-column adjacent rows -> 3, rows between
// plus a column hop -> 5.
eq(outSegs(AOUTS[0]).length, 5, "same-col span rides 5 segs");
ok(AOUTS[1].indexOf("<line") === 0, "adjacent rows stay straight");
eq(JSON.stringify(outSegs(AOUTS[1])), JSON.stringify([[44, 40, 44, 102]]),
  "adjacent rows draw one vertical");
eq(outSegs(AOUTS[7]).length, 3, "adjacent rows + col hop rides 3");
eq(outSegs(AOUTS[3]).length, 5, "rows between + col hop rides 5");
eq(diagLines(AOUTS).length, 0, "no diagonals when gated");
eq(throughNode(AOUTS, AEDGES, APOS).length, 0,
  "0 through-node, search gated :: " +
  JSON.stringify(throughNode(AOUTS, AEDGES, APOS).slice(0, 2)));
console.log("PASS " + pass);
''';

L10F_CHECKS_B = r'''
// (b) wide focus hub: the center's 25 relations ride distinct
// gutter lanes; neighbor-neighbor edges hide without a selection.
gMode = "focus"; ovNarrow = false; narrowTree = false;
gapRows = laneGapRows(Object.values(HPOS), nodeR);
eq(gapRows.length, 14, "center row plus 13 grid rows");
narrowCol = false;
var HUNG = HEDGES.map(e => __renderEdge(e, HPOS));
ok(throughNode(HUNG, HEDGES, HPOS).length > 0, "ungated hub crosses");
var scH = laneScope(false, false, "focus", true, null, "C", false);
eq(scH.col, true, "wide focus gates");
eq(scH.sel, "C", "no selection -> the center is distinguished");
narrowCol = scH.col; laneSel = scH.sel;
relLanes = incidentLaneMap(HEDGES, HPOS, laneSel, nodeR, false);
eq(relLanes.size, 25, "the 25 center relations get lanes");
var hlx = [];
relLanes.forEach(lx => hlx.push(lx));
eq(new Set(hlx).size, 25, "lanes distinct per gutter");
eq(hlx[0], 16, "first lane at the gutter edge");
eq(hlx[24], 25, "last lane inside the gutter");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var HOUTS = HEDGES.map(e => __renderEdge(e, HPOS));
eq(HOUTS.filter(o => o === "").length, 2, "neighbor pairs hidden");
eq(HOUTS[25], "", "N0-N5 hidden");
eq(HOUTS[26], "", "N3-N4 hidden");
eq(hiddenRels, 2, "hidden counter");
eq(drawnRels, 25, "drawn counter");
eq(relationHintText(hiddenRels, drawnRels, "중심 노드"),
  "관계선 2개 숨김 — 중심 노드의 관계 25개 표시 중", "hub hint text");
for (var hi = 0; hi < 25; hi++) {
  ok(HOUTS[hi].indexOf("<path") === 0, "center edge rides a path :: " + hi);
  var nseg = outSegs(HOUTS[hi]).length;
  ok(nseg === 3 || nseg === 5, "3 or 5 segs :: " + hi);
}
eq(outSegs(HOUTS[0]).length, 3, "first-row center edge rides 3");
eq(outSegs(HOUTS[24]).length, 5, "last-row center edge rides 5");
eq(diagLines(HOUTS).length, 0, "no diagonals when gated");
eq(throughNode(HOUTS, HEDGES, HPOS).length, 0,
  "0 through-node, hub gated :: " +
  JSON.stringify(throughNode(HOUTS, HEDGES, HPOS).slice(0, 2)));
console.log("PASS " + pass);
''';

L10F_CHECKS_CW = r'''
// (c) mode=all wide: gated like search, honest hint both ways.
gMode = "all"; ovNarrow = false; narrowTree = false;
gapRows = laneGapRows(Object.values(WPOS2), nodeR);
eq(gapRows.length, 3, "three grid rows");
narrowCol = false;
var WUNG = WEDGES2.map(e => __renderEdge(e, WPOS2));
var WUNGBAD = throughNode(WUNG, WEDGES2, WPOS2);
eq(WUNGBAD.length, L10F_W_UNGATED_THROUGH, "frozen before-count");
// No selection: every relation hides behind the honest count.
var scW0 = laneScope(false, false, "all", false, null, null, false);
eq(scW0.col, true, "all gates at wide widths");
eq(scW0.sel, null, "no scope without a selection");
narrowCol = scW0.col; laneSel = scW0.sel;
relLanes = incidentLaneMap(WEDGES2, WPOS2, laneSel, nodeR, false);
eq(relLanes.size, 0, "nothing incident without a selection");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var WOUTS0 = WEDGES2.map(e => __renderEdge(e, WPOS2));
eq(WOUTS0.filter(o => o === "").length, 4, "all hidden");
eq(hiddenRels, 4, "hidden counter");
eq(drawnRels, 0, "drawn counter");
eq(relationHintText(hiddenRels, drawnRels, "선택한 노드"),
  "관계선 4개 숨김 — 노드를 선택하면 그 노드의 관계만 표시합니다",
  "all hint, no selection");
// A selection narrows to that node.
var scW = laneScope(false, false, "all", false, "A0", null, false);
eq(scW.col, true, "selection keeps the gate");
eq(scW.sel, "A0", "selection is the scope");
narrowCol = scW.col; laneSel = scW.sel;
relLanes = incidentLaneMap(WEDGES2, WPOS2, laneSel, nodeR, false);
eq(relLanes.size, 2, "the 2 A0 relations get lanes");
var wlx = [];
relLanes.forEach(lx => wlx.push(lx));
eq(JSON.stringify(wlx), JSON.stringify([16, 22]), "lanes distinct");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var WOUTS = WEDGES2.map(e => __renderEdge(e, WPOS2));
eq(WOUTS.filter(o => o === "").length, 2, "non-A0 relations hidden");
eq(WOUTS[2], "", "B0-B2 hidden");
eq(WOUTS[3], "", "A1-B1 hidden");
eq(hiddenRels, 2, "hidden counter follows the selection");
eq(drawnRels, 2, "drawn counter follows the selection");
eq(relationHintText(hiddenRels, drawnRels, "선택한 노드"),
  "관계선 2개 숨김 — 선택한 노드의 관계 2개 표시 중",
  "all hint, selection");
eq(outSegs(WOUTS[0]).length, 5, "same-col span rides 5 segs");
eq(outSegs(WOUTS[1]).length, 3, "adjacent rows + col hop rides 3");
eq(diagLines(WOUTS).length, 0, "no diagonals when gated");
eq(throughNode(WOUTS, WEDGES2, WPOS2).length, 0,
  "0 through-node, all wide gated :: " +
  JSON.stringify(throughNode(WOUTS, WEDGES2, WPOS2).slice(0, 2)));
console.log("PASS " + pass);
''';

L10F_CHECKS_CN = r'''
// (c) mode=all narrow: the single column gates too.
gMode = "all"; ovNarrow = true; narrowTree = false;
gapRows = laneGapRows(Object.values(NPOS), nodeR);
eq(gapRows.length, 5, "five column rows");
narrowCol = false;
var NUNG = NEDGES.map(e => __renderEdge(e, NPOS));
var NUNGBAD = throughNode(NUNG, NEDGES, NPOS);
eq(NUNGBAD.length, L10F_N_UNGATED_THROUGH, "frozen before-count");
var scN0 = laneScope(false, true, "all", false, null, null, false);
eq(scN0.col, true, "all gates at narrow widths");
eq(scN0.sel, null, "no scope without a selection");
narrowCol = scN0.col; laneSel = scN0.sel;
relLanes = incidentLaneMap(NEDGES, NPOS, laneSel, nodeR, false);
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var NOUTS0 = NEDGES.map(e => __renderEdge(e, NPOS));
eq(NOUTS0.filter(o => o === "").length, 4, "all hidden");
eq(relationHintText(hiddenRels, drawnRels, "선택한 노드"),
  "관계선 4개 숨김 — 노드를 선택하면 그 노드의 관계만 표시합니다",
  "all narrow hint, no selection");
var scN = laneScope(false, true, "all", false, "N0", null, false);
eq(scN.col, true, "selection keeps the gate");
eq(scN.sel, "N0", "selection is the scope");
narrowCol = scN.col; laneSel = scN.sel;
relLanes = incidentLaneMap(NEDGES, NPOS, laneSel, nodeR, false);
eq(relLanes.size, 2, "the 2 N0 relations get lanes");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var NOUTS = NEDGES.map(e => __renderEdge(e, NPOS));
eq(NOUTS.filter(o => o === "").length, 2, "non-N0 relations hidden");
eq(relationHintText(hiddenRels, drawnRels, "선택한 노드"),
  "관계선 2개 숨김 — 선택한 노드의 관계 2개 표시 중",
  "all narrow hint, selection");
eq(outSegs(NOUTS[0]).length, 5, "span rides 5 segs");
eq(outSegs(NOUTS[2]).length, 5, "skip-one rides 5 segs");
eq(diagLines(NOUTS).length, 0, "no diagonals when gated");
eq(throughNode(NOUTS, NEDGES, NPOS).length, 0,
  "0 through-node, all narrow gated :: " +
  JSON.stringify(throughNode(NOUTS, NEDGES, NPOS).slice(0, 2)));
console.log("PASS " + pass);
''';

L10F_CHECKS_G = r'''
// Group: the repo spine elbows stay byte-identical structure;
// member pairs gate and ride the gap route.
gMode = "group"; ovNarrow = false; narrowTree = false;
gGroupSpine = {x: 60, top: 46, bottom: 314};
gapRows = laneGapRows(Object.values(GPOS), nodeR);
eq(gapRows.length, 5, "repo row plus four member rows");
var scG = laneScope(false, false, "group", false, null, null, false);
eq(scG.col, true, "group gates");
eq(scG.sel, null, "no scope without a selection");
narrowCol = scG.col; laneSel = scG.sel;
var relG0 = incidentLaneMap(GEDGES, GPOS, null, nodeR, true);
eq(relG0.size, 0, "nothing incident without a selection");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var GOUTS0 = GEDGES.map(e => __renderEdge(e, GPOS));
ok(GOUTS0[0].indexOf("<path") === 0, "repo elbow still draws");
ok(GOUTS0[1].indexOf("<path") === 0, "repo elbow still draws");
eq(GOUTS0[2], "", "member pair hidden");
eq(GOUTS0[3], "", "member pair hidden");
eq(GOUTS0[0],
  '<path d="M 60 128 H 82" fill="none" stroke="#88888855" stroke-width="1">' +
  '<title>contains · </title></path>',
  "spine elbow byte-identical");
var scG2 = laneScope(false, false, "group", false, "M0", null, false);
eq(scG2.col, true, "selection keeps the gate");
eq(scG2.sel, "M0", "selection is the scope");
narrowCol = scG2.col; laneSel = scG2.sel;
// The product passes withSpine=true for group (the mirror matches
// the group-spine branch): the repo edges take no lane slots.
relLanes = incidentLaneMap(GEDGES, GPOS, laneSel, nodeR, true);
eq(relLanes.size, 1, "only the M0 pair gets a lane");
ok(!relLanes.has(GEDGES[0]), "spine-taker takes no slot");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var GOUTS = GEDGES.map(e => __renderEdge(e, GPOS));
eq(GOUTS[3], "", "other pair stays hidden");
eq(outSegs(GOUTS[2]).length, 5, "member span rides 5 segs");
eq(diagLines(GOUTS).length, 0, "no diagonals when gated");
eq(throughNode(GOUTS, GEDGES, GPOS).length, 0,
  "0 through-node, group gated :: " +
  JSON.stringify(throughNode(GOUTS, GEDGES, GPOS).slice(0, 2)));
console.log("PASS " + pass);
''';

L10F_CHECKS_SCOPE = r'''
// Scope table: [narrowTree, ovNarrow, mode, focusValid, sel, focus,
// hasQuery, col, outSel]. The narrow rows keep their 253/255 scope;
// every other column/grid layout joins the gate in 257.
[
  [true, true, "overview", false, null, null, false, true, null],
  [true, true, "overview", false, "S", null, true, true, "S"],
  [false, true, "focus", true, null, "F", false, true, "F"],
  [false, true, "focus", true, "S", "F", true, true, "S"],
  [false, false, "focus", true, null, "C", false, true, "C"],
  [false, false, "focus", true, "S", "C", true, true, "S"],
  [false, false, "focus", false, null, null, false, false, null],
  [false, false, "group", false, null, null, false, true, null],
  [false, true, "group", false, "M", null, false, true, "M"],
  [false, false, "all", false, null, null, false, true, null],
  [false, true, "all", false, "S", null, false, true, "S"],
  [false, false, "overview", false, "S", null, true, true, "S"],
  [false, false, "overview", false, "S", null, false, false, null]
].forEach((r, i) => {
  var got = laneScope(r[0], r[1], r[2], r[3], r[4], r[5], r[6]);
  eq(got.col, r[7], "scope col :: " + i);
  eq(got.sel, r[8], "scope sel :: " + i);
});
console.log("PASS " + pass);
''';

L10F_HUBQ_NODES = r'''
var HQ = [{id: "issue:C", type: "issue", label: "hub center"}];
var HQE = [];
for (var qi = 0; qi < 25; qi++) {
  HQ.push({id: "run:Y" + qi, type: "run", label: "ctx item " + qi});
  HQE.push({from: "issue:C", to: "run:Y" + qi, kind: "about",
    provenance: ""});
}
''';

L10F_CHECKS_BSUB = r'''
// (b) the hub subtitle and fold note keep their 255 forms.
var hq = focusQueryShow(HQ, HQE, "issue:C", "hub");
eq(hq.centerHit, true, "center matches");
eq(hq.hitN, 0, "no neighbor matches");
eq(hq.ctxN, 25, "all neighbors are context");
eq(hq.poolN, 25, "pre-cap pool stated");
eq(hq.hitN + hq.ctxN, hq.poolN, "invariant, hub");
eq(hq.show.length, 21, "display cap still 21");
eq(hq.show[0].id, "issue:C", "center stays first");
eq(hq.folded, '검색 "hub" 일치 0건 + 이웃 — 25건 중 20건만 표시',
  "hub fold note exact");
eq(focusQuerySub("hub center", hq.fullN, hq.hitN, hq.centerHit, hq.ctxN, "hub", true),
  'hub center — 검색 "hub"에 일치하는 노드라 이웃 25건 중 20건을 봅니다 (이웃 중 일치 0건, 일치 먼저)',
  "hub capped subtitle exact");
var hq0 = focusQueryShow(HQ, HQE, "issue:C", "");
eq(hq0.show.length, 21, "no-q cap still 21");
eq(hq0.folded, "이웃 25건 중 20건만 표시", "no-q fold note unchanged");
eq(hq0.hitN + hq0.ctxN, hq0.poolN, "invariant, hub no-q");
console.log("PASS " + pass);
''';

L10F_HASH_STUBS = r'''
var location = {hash: ""};
var gClientType = "", gClientStatus = "";
var gMode = "overview", gFocus = null, graphSel = null;
var gQuery = "", gNeedFit = false;
var gDetailKey = null, gZeroNotice = false;
var __box = {value: "old"};
var document = {activeElement: "MARKER",
  getElementById: function(id) { return id === "g-q" ? __box : null; },
  querySelector: function(sel) { return null; }};
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L10F_CHECKS_D = r'''
// (d) an in-graph hash change syncs the search box value without
// stealing focus; a missing box is a silent no-op.
gMode = "overview"; gFocus = null; graphSel = null; gQuery = "old";
__box.value = "old"; document.activeElement = "MARKER";
location.hash = "#/graph?q=hub";
var dv = parseHash();
applyGraphHashState(dv);
eq(gQuery, "hub", "q applies");
eq(__box.value, "hub", "search box follows the hash");
eq(document.activeElement, "MARKER", "focus untouched");
location.hash = "#/graph";
dv = parseHash();
applyGraphHashState(dv);
eq(gQuery, "", "absent q clears");
eq(__box.value, "", "search box clears too");
eq(document.activeElement, "MARKER", "focus still untouched");
// The 255 (c) restore keeps working with the box stubbed.
gMode = "focus"; gFocus = "issue:X"; graphSel = "issue:X";
gQuery = "issue"; gDetailKey = "x"; __box.value = "issue";
location.hash = "#/graph?q=issue";
dv = parseHash();
applyGraphHashState(dv);
eq(gMode, "overview", "modeless hash restores overview");
eq(gFocus, null, "stale focus cleared");
eq(__box.value, "issue", "box keeps the kept q");
ok(!(gMode === "focus" && gFocus == null), "never stranded");
console.log("PASS " + pass);
''';

L10F_CHECKS_GAP = r'''
// Gap-row collection and gap-lane geometry, pure units.
var GNODES = [
  {id: "a", type: "run", _x: 44, _y: 102},
  {id: "b", type: "run", _x: 294, _y: 40},
  {id: "c", type: "repo", _x: 60, _y: 40},
  {id: "d", type: "run", _x: 44, _y: NaN},
  {id: "e", type: "run", _x: 44}
];
var grows = laneGapRows(GNODES, nodeR);
eq(grows.length, 2, "two finite rows");
eq(grows[0].y, 40, "rows sorted");
eq(grows[0].r, 14, "repo clearance wins the row");
eq(grows[1].y, 102, "second row");
eq(grows[1].r, 10, "run clearance");
eq(JSON.stringify(laneGapRows([], nodeR)), "[]", "empty stays empty");
// Adjacent rows across columns: one V stub each plus the gap H.
var AROWS = [{y: 40, r: 10}, {y: 102, r: 10}];
eq(gapLaneD(34, 40, 284, 102, 16, AROWS),
  "M 34 40 V 71 H 284 V 102", "adjacent rows ride 3 segs");
// Rows between: stub, gap H, gutter V, gap H, stub.
var BROWS = [{y: 40, r: 10}, {y: 102, r: 10}, {y: 164, r: 10}];
eq(gapLaneD(34, 40, 34, 164, 19, BROWS),
  "M 34 40 V 71 H 19 V 133 H 34 V 164", "rows between ride 5 segs");
// Same row: a U through the next gap below.
eq(gapLaneD(34, 102, 284, 102, 22, BROWS),
  "M 34 102 V 133 H 284 V 102", "same row rides a U below");
// Same row, last row: the U goes through the gap above.
eq(gapLaneD(34, 164, 284, 164, 22, BROWS),
  "M 34 164 V 133 H 284 V 164", "last-row U goes above");
// A lone row: the U clears by a full pitch.
eq(gapLaneD(34, 40, 284, 40, 16, [{y: 40, r: 10}]),
  "M 34 40 V 68 H 284 V 40", "lone-row U");
// The tightest real pitch (overview repo row 26px under its first
// grid row) still clears both circles.
var TROWS = [{y: 20, r: 10}, {y: 46, r: 14}];
var tight = gapLaneD(50, 20, 310, 46, 16, TROWS);
eq(tight, "M 50 20 V 31 H 310 V 46", "tight gap rides 3 segs");
var gy = 31;
ok(Math.abs(gy - 20) >= 10 && Math.abs(gy - 46) >= 14,
  "tight gap clears both rows");
// The gutter band sits left of every grid column.
var lefts = [44 - 9, 46 - 9, 60 - 13, 92 - 9, 296 - 9, 316 - 9];
ok(Math.min.apply(null, lefts) > 25, "gutter clears all columns");
console.log("PASS " + pass);
''';

L10F_CHECKS_L10G = r'''
// L10g: the focus center label always sits right of its circle,
// where the left-going lane stubs can never reach it.
var LONG = "a very long center label that must flip sides somewhere";
var leftSide = graphLabelSide(LONG, 163, 326, false);
var rightSide = graphLabelSide(LONG, 163, 326, true);
eq(leftSide.anchor, "end", "grid rule flips a long center label");
eq(rightSide.anchor, "start", "forced right stays right");
eq(leftSide.avail, rightSide.avail, "center avail is symmetric");
// The lane's top horizontal (M cx-r-1 cy H lx) spans at most
// [16, cx-r-1]; the right label starts at cx+16.
var laneD = relationLaneD(153, 46, 36, 130, 16);
eq(laneD, "M 153 46 H 16 V 130 H 36", "center lane shape");
ok(153 < 163 + 16, "lane stays left of the right label edge");
console.log("PASS " + pass);
''';


def _l10f_script(checks, extra_stubs="", extra_decls=(), extra_fixtures="",
                 lane_decls=("incidentLaneMap", "relationLaneX", "relationLaneD",
                             "relationHintText", "laneScope")):
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
    parts = [L10F_FRAG_STUBS + extra_stubs,
             "const nodeR = %s;" % m.group(1), mrow.group(0),
             _decl(src, "leftoverLaneD")]
    for name in lane_decls + tuple(extra_decls):
        parts.append(_decl(src, name))
    parts.append(L10F_GEO)
    parts.append("function __renderEdge(e, pos) {\n" + edge + "\n}")
    parts.append(extra_fixtures)
    parts.append(checks)
    return "\n".join(parts)


# Frozen before-counts: ungated straight-line through-node hits per
# fixture, captured pre-change by the muse257-capture throwaway. The
# ungated branch is untouched by 257, so these hold before and after.
L10F_A_UNGATED_THROUGH = 2
L10F_W_UNGATED_THROUGH = 2
L10F_N_UNGATED_THROUGH = 4

# (e) narrow goldens: captured pre-change by the muse257-capture
# throwaway, then frozen. 257 must not move a single character.
L10F_NARROW_TREE_GOLDEN = [
    '<path d="M 40 62 V 94 H 78" fill="none" stroke="#88888855" stroke-width="1"><title>contains · </title></path>',
    '<path d="M 40 62 V 156 H 78" fill="none" stroke="#8fb4e8" stroke-width="2"><title>contains · </title></path>',
    '<line x1="88" y1="94" x2="88" y2="156" stroke="#8fb4e8" stroke-width="2"><title>related · </title></line>',
    '<path d="M 30 218 H 22 V 156 H 78" fill="none" stroke="#8fb4e8" stroke-width="2"><title>works-on · </title></path>',
]
L10F_NARROW_FOCUS_GOLDEN = [
    '<path d="M 153 46 H 16 V 130 H 36" fill="none" stroke="#88888855" stroke-width="1"><title>about · </title></path>',
    '<path d="M 153 46 H 22 V 192 H 36" fill="none" stroke="#88888855" stroke-width="1"><title>about · </title></path>',
    '',
]


def _l10f_golden_outputs(fixture, state_lines, map_expr):
    """Render a narrow fixture through the REAL edge fragment."""
    script = _l10f_script("var OUTS = %s;\nconsole.log(JSON.stringify(OUTS));"
                          % map_expr,
                          extra_fixtures=fixture + "\n" + state_lines)
    proc = _run_node(script)
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class L10fWideSearchListTest(unittest.TestCase):
    # (a) wide search list: 0 through-node after, distinct lanes,
    # orthogonal shapes; the RED before-count is frozen.

    def test_search_list_gates_and_routes_clean(self):
        src = _script()
        for name in ("laneScope", "incidentLaneMap", "relationLaneX",
                     "laneGapRows", "gapLaneD", "relationHintText"):
            self.assertIn("function %s(" % name, src,
                          "missing L10f helper " + name)
        self.assertGreaterEqual(L10F_A_UNGATED_THROUGH, 0,
                                "before-count not captured")
        script = _l10f_script(
            L10F_CHECKS_A,
            extra_stubs="\nvar L10F_A_UNGATED_THROUGH = %d;"
            % L10F_A_UNGATED_THROUGH,
            extra_decls=("laneGapRows", "gapLaneD"),
            extra_fixtures=L10F_SEARCH_POS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10f(a) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10fWideFocusHubTest(unittest.TestCase):
    # (b) wide focus hub: 25 center relations on distinct lanes,
    # 0 through-node; subtitle and fold keep their 255 forms.

    def test_hub_gates_and_routes_clean(self):
        src = _script()
        self.assertIn("function gapLaneD(", src, "missing gap router")
        script = _l10f_script(
            L10F_CHECKS_B,
            extra_decls=("laneGapRows", "gapLaneD"),
            extra_fixtures=L10F_HUB_POS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10f(b) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_hub_subtitle_and_fold_unchanged(self):
        src = _script()
        for name in ("focusQueryShow", "focusQuerySub", "graphSearchHits"):
            self.assertIn("function %s(" % name, src,
                          "missing subtitle helper " + name)
        script = (L10F_FRAG_STUBS + "\n" + _decl(src, "graphSearchHits")
                  + "\n" + _decl(src, "focusQueryShow")
                  + "\n" + _decl(src, "focusQuerySub")
                  + "\n" + L10F_HUBQ_NODES + "\n" + L10F_CHECKS_BSUB)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10f(b-sub) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10fAllAndScopeTest(unittest.TestCase):
    # (c) mode=all wide and narrow: 0 through-node and an honest
    # hint; group elbows intact; the scope table.

    def test_all_wide_gates_with_honest_hint(self):
        self.assertGreaterEqual(L10F_W_UNGATED_THROUGH, 0,
                                "before-count not captured")
        script = _l10f_script(
            L10F_CHECKS_CW,
            extra_stubs="\nvar L10F_W_UNGATED_THROUGH = %d;"
            % L10F_W_UNGATED_THROUGH,
            extra_decls=("laneGapRows", "gapLaneD"),
            extra_fixtures=L10F_ALLW_POS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10f(c-w) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_all_narrow_gates_with_honest_hint(self):
        self.assertGreaterEqual(L10F_N_UNGATED_THROUGH, 0,
                                "before-count not captured")
        script = _l10f_script(
            L10F_CHECKS_CN,
            extra_stubs="\nvar L10F_N_UNGATED_THROUGH = %d;"
            % L10F_N_UNGATED_THROUGH,
            extra_decls=("laneGapRows", "gapLaneD"),
            extra_fixtures=L10F_ALLN_POS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10f(c-n) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_group_elbows_intact_members_gate(self):
        src = _script()
        self.assertIn("function elbowBranchD(", src, "missing elbow helper")
        paint = _fn_body(src, "paintGraph")
        self.assertIn("narrowTree || gMode === \"group\"", paint,
                      "lane mirror must match the group-spine branch")
        script = _l10f_script(
            L10F_CHECKS_G,
            extra_decls=("laneGapRows", "gapLaneD", "elbowBranchD"),
            extra_fixtures=L10F_GROUP_POS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10f(group) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_lane_scope_table(self):
        src = _script()
        script = (L10F_FRAG_STUBS + "\n" + _decl(src, "laneScope")
                  + "\n" + L10F_CHECKS_SCOPE)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10f(scope) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10fInputSyncTest(unittest.TestCase):
    # (d) an in-graph hash change syncs #g-q without stealing focus.

    def test_hash_change_syncs_search_box(self):
        src = _script()
        for name in ("parseHash", "clearGraphPanel", "applyGraphHashState"):
            self.assertIn("function %s(" % name, src,
                          "missing hash helper " + name)
        script = (L10F_HASH_STUBS + "\n" + _decl(src, "parseHash")
                  + "\n" + _decl(src, "clearGraphPanel")
                  + "\n" + _decl(src, "applyGraphHashState")
                  + "\n" + L10F_CHECKS_D)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10f(d) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10fGapUnitsTest(unittest.TestCase):
    # Pure gap-row and gap-lane geometry.

    def test_gap_rows_and_lane_shapes(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        m = re.search(r"const nodeR = (n => [^;]+);", paint)
        assert m, "nodeR radius rule missing"
        script = (L10F_FRAG_STUBS + "\nconst nodeR = %s;" % m.group(1)
                  + "\n" + _decl(src, "laneGapRows")
                  + "\n" + _decl(src, name="gapLaneD")
                  + "\n" + L10F_CHECKS_GAP)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10f(gap) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10fCenterLabelLaneTest(unittest.TestCase):
    # L10g: the focus center label sits right, clear of the lanes.

    def test_center_label_right_and_clear(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        self.assertIn("|| (gMode === \"focus\" && n.id === gFocus)", paint,
                      "center label rule must be in paint")
        for name in ("gLabelW", "graphLabelSide", "relationLaneD"):
            self.assertIn("function %s(" % name, src,
                          "missing label helper " + name)
        script = (L10F_FRAG_STUBS + "\n" + _decl(src, "gLabelW")
                  + "\n" + _decl(src, "graphLabelSide")
                  + "\n" + _decl(src, "relationLaneD")
                  + "\n" + L10F_CHECKS_L10G)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10g failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10fNarrowUnchangedTest(unittest.TestCase):
    # (e) 253/255 outputs unchanged: the wide goldens reused by
    # reference, the narrow shapes frozen from the pre-change tree.

    def test_wide_goldens_reused(self):
        from tests.test_251_followups import (
            L10CD_WIDE_GOLDEN, _l10cd_wide_outputs)
        from tests.test_255_followups import (
            L10E_WIDE_FOCUS_GOLDEN, _l10e_wide_focus_outputs)
        self.assertTrue(L10CD_WIDE_GOLDEN, "253 golden missing")
        self.assertEqual(_l10cd_wide_outputs(), L10CD_WIDE_GOLDEN)
        self.assertTrue(L10E_WIDE_FOCUS_GOLDEN, "255 golden missing")
        outs, bad = _l10e_wide_focus_outputs()
        self.assertEqual(outs, L10E_WIDE_FOCUS_GOLDEN)
        self.assertEqual(bad, [], "wide fixture stays clean")

    def test_narrow_tree_frozen(self):
        self.assertTrue(L10F_NARROW_TREE_GOLDEN, "tree golden not captured")
        outs = _l10f_golden_outputs(
            L10F_NARROW_TREE_POS,
            'graphSel = "K1"; narrowTree = true; narrowCol = true;\n'
            'gMode = "overview"; ovNarrow = true;\n'
            'relLanes = incidentLaneMap(TEDGES, TPOS, "K1", nodeR, true);\n'
            "hiddenRels = 0; drawnRels = 0; relHl = new Set();",
            "TEDGES.map(e => __renderEdge(e, TPOS))")
        self.assertEqual(outs, L10F_NARROW_TREE_GOLDEN)

    def test_narrow_focus_frozen(self):
        self.assertTrue(L10F_NARROW_FOCUS_GOLDEN, "focus golden not captured")
        outs = _l10f_golden_outputs(
            L10F_NARROW_FOCUS_POS,
            'graphSel = null; gFocus = "F"; narrowTree = false;\n'
            'gMode = "focus"; ovNarrow = true; narrowCol = true;\n'
            'relLanes = incidentLaneMap(QEDGES2, QPOS, "F", nodeR, false);\n'
            "hiddenRels = 0; drawnRels = 0; relHl = new Set();",
            "QEDGES2.map(e => __renderEdge(e, QPOS))")
        self.assertEqual(outs, L10F_NARROW_FOCUS_GOLDEN)


if __name__ == "__main__":
    unittest.main()





