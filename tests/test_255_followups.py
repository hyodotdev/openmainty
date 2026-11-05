"""Gate-3 live-check followups (255): L10e focus+search column lanes.

Focus mode at narrow widths lays out a single column but, unlike the
253 overview tree, draws every relation as a straight line through
the column. This file pins the L10e rule with real dashboard
functions under node: q narrows within the focus neighborhood, every
narrow column layout gates relations to the selected-or-focused node
on distinct lanes with zero through-node intersections, and a
modeless hash never strands focus mode. Product constants extracted
verbatim, never hand-mirrored.
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


L10E_FRAG_STUBS = r'''
var graphSel = null, gFocus = null;
var gGroupSpine = null;
var narrowTree = false, narrowCol = false, laneSel = null;
var gMode = "focus", ovNarrow = true;
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

L10E_FOCUS_POS = r'''
// Narrow focus shape: center on top, neighbors in one column below
// (grid single column at x=46, ROWH pitch), plus a repo neighbor so
// the spine mirror can be tested inverted (no spine in focus mode).
var FPOS = {
  F: {id: "issue:F", type: "issue", _x: 163, _y: 46},
  P: {id: "repo:P", type: "repo", _x: 46, _y: 130},
  N0: {id: "run:N0", type: "run", _x: 46, _y: 192},
  N1: {id: "run:N1", type: "run", _x: 46, _y: 254},
  N2: {id: "branch:N2", type: "branch", _x: 46, _y: 316},
  N3: {id: "run:N3", type: "run", _x: 46, _y: 378},
  N4: {id: "question:N4", type: "question", _x: 46, _y: 440},
  N5: {id: "run:N5", type: "run", _x: 46, _y: 502}
};
// Indices 0-3 touch the center F; 4-6 do not.
var FEDGES = [
  {from: "P", to: "F", kind: "related", provenance: ""},
  {from: "F", to: "N0", kind: "about", provenance: ""},
  {from: "F", to: "N1", kind: "about", provenance: ""},
  {from: "N2", to: "F", kind: "works-on", provenance: ""},
  {from: "N0", to: "N4", kind: "related", provenance: ""},
  {from: "N1", to: "N2", kind: "related", provenance: ""},
  {from: "N3", to: "N5", kind: "related", provenance: ""}
];
''';

L10E_GEO = r'''
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
''';

L10E_CHECKS_A = r'''
// (a) focus + q at narrow: the focused node's relations ride
// distinct lanes, everything else hides, nothing crosses a
// non-endpoint circle.
var sc = laneScope(false, true, "focus", true, null, "F");
eq(sc.col, true, "focus narrow takes a column");
eq(sc.sel, "F", "no selection -> the focus is distinguished");
narrowCol = sc.col;
relLanes = incidentLaneMap(FEDGES, FPOS, sc.sel, nodeR, false);
eq(relLanes.size, 4, "the 4 center relations get lanes");
ok(relLanes.has(FEDGES[0]), "repo edge draws too: no spine in focus");
var lxs = [];
relLanes.forEach(lx => lxs.push(lx));
eq(new Set(lxs).size, 4, "lanes distinct :: " + JSON.stringify(lxs));
eq(JSON.stringify(lxs), JSON.stringify([16, 19, 22, 25]),
  "lanes pack the gutter");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var OUTS = FEDGES.map(e => __renderEdge(e, FPOS));
eq(OUTS.filter(o => o === "").length, 3, "non-center relations hidden");
[4, 5, 6].forEach(i => eq(OUTS[i], "", "hidden :: " + i));
[0, 1, 2, 3].forEach(i => {
  ok(/H [\d.]+ V/.test(OUTS[i]), "center relation rides a lane :: " + OUTS[i]);
});
ok(OUTS.every(o => o.indexOf("<line") !== 0), "no straight at all here");
eq(hiddenRels, 3, "hidden counter");
eq(drawnRels, 4, "drawn counter");
eq(relationHintText(hiddenRels, drawnRels, "중심 노드"),
  "관계선 3개 숨김 — 중심 노드의 관계 4개 표시 중", "focus hint text");
// A selection inside focus narrows to that node, like overview.
var sc2 = laneScope(false, true, "focus", true, "N0", "F");
eq(sc2.col, true, "selection keeps the column");
eq(sc2.sel, "N0", "selection wins over the focus");
relLanes = incidentLaneMap(FEDGES, FPOS, sc2.sel, nodeR, false);
eq(relLanes.size, 2, "only N0's relations get lanes");
hiddenRels = 0; drawnRels = 0; relHl = new Set();
var OUTS2 = FEDGES.map(e => __renderEdge(e, FPOS));
eq(OUTS2.filter(o => o === "").length, 5, "the rest hides");
[1, 4].forEach(i => {
  ok(/H [\d.]+ V/.test(OUTS2[i]), "selected relation rides a lane :: " + i);
});
eq(hiddenRels, 5, "hidden counter follows the selection");
eq(drawnRels, 2, "drawn counter follows the selection");
eq(relationHintText(hiddenRels, drawnRels, "선택한 노드"),
  "관계선 5개 숨김 — 선택한 노드의 관계 2개 표시 중",
  "selection hint text inside focus");
// The spine mirror inverts with the flag: a true spine-taker is
// excluded with a spine, included without one.
var SPOS = {R: {id: "repo:R", type: "repo", _x: 40, _y: 46},
  K: {id: "run:K", type: "run", _x: 88, _y: 156}};
var SEDGES = [{from: "R", to: "K", kind: "contains", provenance: ""}];
eq(incidentLaneMap(SEDGES, SPOS, "K", nodeR, true).size, 0,
  "spine-taker excluded with a spine");
eq(incidentLaneMap(SEDGES, SPOS, "K", nodeR, false).size, 1,
  "spine-taker included without one");
eq(relationHintText(3, 4),
  "관계선 3개 숨김 — 선택한 노드의 관계 4개 표시 중",
  "hint default stays the overview form");
// Scope table: overview-narrow and valid-focus-narrow gate; since
// L10f-wide (req 1) valid focus at any width, group and all join
// the gate too. The wide-overview tree row stays ungated.
[
  [true, true, "overview", false, null, null, true, null],
  [true, true, "overview", false, "S", null, true, "S"],
  [false, true, "focus", true, null, "F", true, "F"],
  [false, true, "focus", true, "S", "F", true, "S"],
  [false, true, "focus", false, null, null, false, null],
  [false, false, "focus", true, null, "F", true, "F"],
  [false, true, "group", false, null, null, true, null],
  [false, true, "all", false, null, null, true, null],
  [false, false, "overview", false, "S", null, false, null]
].forEach((r, i) => {
  var got = laneScope(r[0], r[1], r[2], r[3], r[4], r[5]);
  eq(got.col, r[6], "scope col :: " + i);
  eq(got.sel, r[7], "scope sel :: " + i);
});
eq(throughNode(OUTS, FEDGES, FPOS).length, 0,
  "0 through-node, focus default :: " +
  JSON.stringify(throughNode(OUTS, FEDGES, FPOS).slice(0, 2)));
eq(throughNode(OUTS2, FEDGES, FPOS).length, 0,
  "0 through-node, focus selection :: " +
  JSON.stringify(throughNode(OUTS2, FEDGES, FPOS).slice(0, 2)));
console.log("PASS " + pass);
''';

L10E_QNODES = r'''
var QNODES = [
  {id: "issue:C", type: "issue", label: "widget crash on save"},
  {id: "run:R1", type: "run", label: "widget fix run"},
  {id: "run:R2", type: "run", label: "unrelated nightly"},
  {id: "branch:B1", type: "branch", label: "widget-branch"},
  {id: "question:Q1", type: "question", label: "widget copy question"},
  {id: "run:R3", type: "run", label: "widget followup"},
  {id: "repo:P", type: "repo", label: "sample-repo"}
];
var QEDGES = [
  {from: "repo:P", to: "issue:C", kind: "related", provenance: ""},
  {from: "issue:C", to: "run:R1", kind: "about", provenance: ""},
  {from: "issue:C", to: "run:R2", kind: "about", provenance: ""},
  {from: "issue:C", to: "branch:B1", kind: "about", provenance: ""},
  {from: "run:R1", to: "question:Q1", kind: "blocked-by", provenance: ""},
  {from: "run:R2", to: "run:R3", kind: "related", provenance: ""}
];
function qids(list) { return list.map(n => n.id).sort(); }
// Small star with a match-to-context hop, for the ctxN>0 form.
var CNODES = [
  {id: "issue:C", type: "issue", label: "plain center"},
  {id: "run:H", type: "run", label: "hit run alpha"},
  {id: "run:X", type: "run", label: "ctx peer"}
];
var CEDGES = [
  {from: "issue:C", to: "run:H", kind: "about", provenance: ""},
  {from: "issue:C", to: "run:X", kind: "about", provenance: ""},
  {from: "run:H", to: "run:X", kind: "related", provenance: ""}
];
''';

L10E_CHECKS_B = r'''
// (b) q narrows within the focus neighborhood. Counts are real
// matches: hitN excludes the center, ctxN is shown non-center
// non-matches. Invariant (pre-cap): hitN + ctxN = pool non-center.
function inv(fq, msg) { eq(fq.hitN + fq.ctxN, fq.poolN, msg); }
// No q: the full neighborhood, uncapped here; all shown is context.
var plain = focusQueryShow(QNODES, QEDGES, "issue:C", "");
eq(JSON.stringify(qids(plain.show)),
  JSON.stringify(["branch:B1", "issue:C", "repo:P", "run:R1", "run:R2"]),
  "no-q show is the neighborhood");
eq(plain.showE.length, 4, "no-q edges stay inside");
eq(plain.fullN, 4, "full neighbor count");
eq(plain.folded, "", "no fold note when uncapped");
inv(plain, "invariant, no-q");
// Probe 1: q="issue" matches only the center id: every neighbor is
// context, none is claimed as a match.
var p1 = focusQueryShow(QNODES, QEDGES, "issue:C", "issue");
eq(p1.centerHit, true, "center id matches");
eq(p1.hitN, 0, "neighbor matches are 0, not 4");
eq(p1.ctxN, 4, "all neighbors are context");
inv(p1, "invariant, center-only match");
eq(focusQuerySub("widget crash on save", p1.fullN, p1.hitN, p1.centerHit, p1.ctxN, "issue", false),
  'widget crash on save — 검색 "issue"에 일치하는 노드라 이웃 4건을 모두 봅니다 (이웃 중 일치 0건)',
  "center-match subtitle exact");
// Probe 2: q="crash" matches only the center label: same shape.
var p2 = focusQueryShow(QNODES, QEDGES, "issue:C", "crash");
eq(p2.centerHit, true, "center label matches");
eq(p2.hitN, 0, "neighbor matches are 0, not 4");
eq(p2.ctxN, 4, "all neighbors are context");
inv(p2, "invariant, center-label match");
eq(focusQuerySub("widget crash on save", p2.fullN, p2.hitN, p2.centerHit, p2.ctxN, "crash", false),
  'widget crash on save — 검색 "crash"에 일치하는 노드라 이웃 4건을 모두 봅니다 (이웃 중 일치 0건)',
  "center-label-match subtitle exact");
// Probe 3: q="widget" matches the center plus R1 and B1.
var p3 = focusQueryShow(QNODES, QEDGES, "issue:C", "widget");
eq(p3.centerHit, true, "center matches too");
eq(p3.hitN, 2, "neighbor matches are 2 (R1, B1), not 4");
eq(p3.ctxN, 2, "P and R2 are context");
inv(p3, "invariant, center plus neighbor matches");
eq(focusQuerySub("widget crash on save", p3.fullN, p3.hitN, p3.centerHit, p3.ctxN, "widget", false),
  'widget crash on save — 검색 "widget"에 일치하는 노드라 이웃 4건을 모두 봅니다 (이웃 중 일치 2건)',
  "center-plus-matches subtitle exact");
// Probe 4: q="run" matches R1 and R2, not the center.
var p4 = focusQueryShow(QNODES, QEDGES, "issue:C", "run");
eq(p4.centerHit, false, "center does not match");
eq(p4.hitN, 2, "two real matches");
eq(p4.ctxN, 0, "no extra context here");
eq(JSON.stringify(qids(p4.show)),
  JSON.stringify(["issue:C", "run:R1", "run:R2"]),
  "run show is center plus matches");
eq(p4.showE.length, 2, "run edges stay inside");
inv(p4, "invariant, neighbor-only matches");
eq(focusQuerySub("widget crash on save", p4.fullN, p4.hitN, p4.centerHit, p4.ctxN, "run", false),
  'widget crash on save — 이웃 4건 중 검색 "run" 일치 2건만 봅니다',
  "neighbor-match subtitle drops empty context");
// Hit with a non-hit hop: the ctxN>0 subtitle form.
var cn = focusQueryShow(CNODES, CEDGES, "issue:C", "alpha");
eq(cn.centerHit, false, "ctx case: center does not match");
eq(cn.hitN, 1, "ctx case: one match");
eq(cn.ctxN, 1, "ctx case: one context hop");
eq(cn.showE.length, 3, "ctx case: edges stay inside");
inv(cn, "invariant, match plus context");
eq(focusQuerySub("plain center", cn.fullN, cn.hitN, cn.centerHit, cn.ctxN, "alpha", false),
  'plain center — 이웃 2건 중 검색 "alpha" 일치 1건과 그 이웃 1건만 봅니다',
  "match-plus-context subtitle exact");
// q matches nothing: the center alone, honest zero.
var zero = focusQueryShow(QNODES, QEDGES, "issue:C", "zzz-nope");
eq(JSON.stringify(qids(zero.show)), JSON.stringify(["issue:C"]),
  "no match keeps the center");
eq(zero.hitN, 0, "matched zero");
eq(zero.ctxN, 0, "context zero");
eq(zero.centerHit, false, "center does not match");
eq(zero.showE.length, 0, "no edges for the lone center");
inv(zero, "invariant, zero match");
eq(focusQuerySub("widget crash on save", zero.fullN, zero.hitN, zero.centerHit, zero.ctxN, "zzz-nope", false),
  'widget crash on save — 이웃 4건 중 검색 "zzz-nope" 일치 없음 · 검색을 지우면 이웃 4건을 봅니다',
  "zero-match subtitle exact");
// The 21-cap binds after narrowing, matches first: context nodes
// come before the hits in node order, yet both hits stay visible.
var ord = [{id: "issue:C", type: "issue", label: "plain center"}];
var ordE = [];
for (var i = 0; i < 22; i++) {
  ord.push({id: "run:X" + i, type: "run", label: "ctx item " + i});
  ordE.push({from: "issue:C", to: "run:X" + i, kind: "about",
    provenance: ""});
}
ord.push({id: "run:H1", type: "run", label: "hit alpha"});
ord.push({id: "run:H2", type: "run", label: "hit beta"});
ordE.push({from: "issue:C", to: "run:H1", kind: "about", provenance: ""});
ordE.push({from: "issue:C", to: "run:H2", kind: "about", provenance: ""});
for (var j = 0; j < 12; j++) {
  ordE.push({from: "run:H1", to: "run:X" + j, kind: "related",
    provenance: ""});
  ordE.push({from: "run:H2", to: "run:X" + (j + 12), kind: "related",
    provenance: ""});
}
var co = focusQueryShow(ord, ordE, "issue:C", "hit");
eq(co.hitN, 2, "cap-order: two matches");
eq(co.ctxN, 22, "cap-order: 22 context hops");
eq(co.poolN, 24, "cap-order: pre-cap pool stated");
eq(co.show.length, 21, "display cap still 21");
eq(co.show[0].id, "issue:C", "center stays first under the cap");
eq(co.show[1].id, "run:H1", "first hit survives the cap");
eq(co.show[2].id, "run:H2", "second hit survives the cap");
eq(co.show[20].id, "run:X17", "cap fills with context after hits");
inv(co, "invariant, capped pool");
eq(co.show.length - 1, Math.min(20, co.poolN), "post-cap display rule");
eq(co.folded, '검색 "hit" 일치 2건 + 이웃 — 24건 중 20건만 표시',
  "q fold note exact");
eq(focusQuerySub("plain center", co.fullN, co.hitN, co.centerHit, co.ctxN, "hit", true),
  'plain center — 이웃 24건 중 검색 "hit" 일치 2건과 그 이웃 22건 · 그중 20건을 봅니다',
  "capped match-plus-context subtitle exact");
// All-match cap: the center matches too, order is kept.
var many = [{id: "issue:C", type: "issue", label: "cap center"}];
var manyE = [];
for (var k = 0; k < 25; k++) {
  many.push({id: "run:M" + k, type: "run", label: "cap item " + k});
  manyE.push({from: "issue:C", to: "run:M" + k, kind: "about",
    provenance: ""});
}
var capped = focusQueryShow(many, manyE, "issue:C", "cap");
eq(capped.show.length, 21, "display cap still 21");
eq(capped.show[0].id, "issue:C", "center stays first under the cap");
eq(capped.show[20].id, "run:M19", "cap keeps match order");
eq(capped.hitN, 25, "matched counts pre-cap matches");
eq(capped.ctxN, 0, "no context when all match");
eq(capped.poolN, 25, "pre-cap pool stated");
inv(capped, "invariant, all-match cap");
eq(capped.folded, '검색 "cap" 일치 25건 + 이웃 — 25건 중 20건만 표시',
  "all-match fold note exact");
eq(focusQuerySub("cap center", capped.fullN, capped.hitN, true, capped.ctxN, "cap", true),
  'cap center — 검색 "cap"에 일치하는 노드라 이웃 25건 중 20건을 봅니다 (이웃 중 일치 25건, 일치 먼저)',
  "all-match capped subtitle exact");
// Center match capped: the center label matches, 25 neighbors do
// not. No "모두" claim under the cap.
var cmc = [{id: "issue:C", type: "issue", label: "hit center"}];
var cmcE = [];
for (var m = 0; m < 25; m++) {
  cmc.push({id: "run:Y" + m, type: "run", label: "ctx item " + m});
  cmcE.push({from: "issue:C", to: "run:Y" + m, kind: "about",
    provenance: ""});
}
var cmCap = focusQueryShow(cmc, cmcE, "issue:C", "hit");
eq(cmCap.centerHit, true, "center-match cap: center matches");
eq(cmCap.hitN, 0, "center-match cap: no neighbor matches");
eq(cmCap.ctxN, 25, "center-match cap: all context");
eq(cmCap.show.length, 21, "center-match cap binds");
eq(cmCap.show[0].id, "issue:C", "center stays first");
inv(cmCap, "invariant, center-match cap");
eq(focusQuerySub("hit center", cmCap.fullN, cmCap.hitN, cmCap.centerHit, cmCap.ctxN, "hit", true),
  'hit center — 검색 "hit"에 일치하는 노드라 이웃 25건 중 20건을 봅니다 (이웃 중 일치 0건, 일치 먼저)',
  "center-match capped subtitle exact");
// Non-center all-match cap: the ctxN=0 drop rule under the cap.
var nmc = [{id: "issue:C", type: "issue", label: "plain center"}];
var nmcE = [];
for (var w = 0; w < 25; w++) {
  nmc.push({id: "run:Z" + w, type: "run", label: "cap item " + w});
  nmcE.push({from: "issue:C", to: "run:Z" + w, kind: "about",
    provenance: ""});
}
var nmCap = focusQueryShow(nmc, nmcE, "issue:C", "cap");
eq(nmCap.centerHit, false, "non-center cap: center does not match");
eq(nmCap.hitN, 25, "non-center cap: all match");
eq(nmCap.ctxN, 0, "non-center cap: no context");
eq(nmCap.show.length, 21, "non-center cap binds");
inv(nmCap, "invariant, non-center cap");
eq(focusQuerySub("plain center", nmCap.fullN, nmCap.hitN, nmCap.centerHit, nmCap.ctxN, "cap", true),
  'plain center — 이웃 25건 중 검색 "cap" 일치 25건 · 그중 20건을 봅니다',
  "non-center capped subtitle drops empty context");
var cappedPlain = focusQueryShow(many, manyE, "issue:C", "");
eq(cappedPlain.show.length, 21, "no-q cap still 21");
eq(cappedPlain.folded, "이웃 25건 중 20건만 표시",
  "no-q fold note unchanged");
inv(cappedPlain, "invariant, no-q cap");
console.log("PASS " + pass);
''';

L10E_HASH_STUBS = r'''
var location = {hash: ""};
var gClientType = "", gClientStatus = "";
var gMode = "overview", gFocus = null, graphSel = null;
var gQuery = "", gNeedFit = false;
var gDetailKey = null, gZeroNotice = false;
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function stranded() { return gMode === "focus" && gFocus == null; }
''';

L10E_CHECKS_C = r'''
// (c) a modeless hash after focus returns to overview and clears
// the stale focus/selection; a mode-ful hash is authoritative.
// Stranded focus + modeless hash with q.
gMode = "focus"; gFocus = "issue:X"; graphSel = "issue:X";
gQuery = "issue"; gNeedFit = false; gDetailKey = "x";
location.hash = "#/graph?q=issue";
var v = parseHash();
eq(v.kind, "graph", "hash kind parses");
eq(v.mode, "", "modeless hash carries no mode");
applyGraphHashState(v);
eq(gMode, "overview", "modeless hash restores overview");
eq(gFocus, null, "stale focus cleared");
eq(graphSel, null, "stale selection cleared");
eq(gQuery, "issue", "q preserved across the restore");
eq(gNeedFit, true, "restored view refits");
eq(gDetailKey, null, "panel key reset with the selection");
ok(!stranded(), "never focus with null focus");
// Mode-ful focus hash stays authoritative.
gMode = "overview"; gFocus = null; graphSel = null; gQuery = "";
location.hash = "#/graph?mode=focus&focus=issue%3AX&q=issue";
v = parseHash();
applyGraphHashState(v);
eq(gMode, "focus", "mode-ful hash sets focus");
eq(gFocus, "issue:X", "focus id decodes");
eq(gQuery, "issue", "q applies");
eq(graphSel, null, "selection untouched by the hash");
ok(!stranded(), "mode-ful focus is consistent");
// Modeless from overview changes nothing state-wise.
gMode = "overview"; gFocus = null; graphSel = "S"; gQuery = "q1";
location.hash = "#/graph?q=q1";
v = parseHash();
applyGraphHashState(v);
eq(gMode, "overview", "overview stays overview");
eq(graphSel, "S", "overview selection kept");
eq(gQuery, "q1", "q kept");
ok(!stranded(), "overview never strands");
// Stranded group resets the same way.
gMode = "group"; gFocus = "agg:r:issue"; graphSel = "agg:r:issue";
gQuery = "issue";
location.hash = "#/graph";
v = parseHash();
applyGraphHashState(v);
eq(gMode, "overview", "group stranding restores overview");
eq(gFocus, null, "group focus cleared");
eq(gQuery, "", "absent q clears");
ok(!stranded(), "group never strands");
console.log("PASS " + pass);
''';

L10E_WIDE_POS = r'''
// Wide focus shape: center on top, neighbors in a 3-column grid
// (panelW 900: center 450, grid x=46/296/546, ROWH pitch).
var WPOS = {
  F: {id: "issue:F", type: "issue", _x: 450, _y: 46},
  A: {id: "run:A", type: "run", _x: 46, _y: 130},
  B: {id: "run:B", type: "run", _x: 296, _y: 130},
  C: {id: "branch:C", type: "branch", _x: 546, _y: 130},
  D: {id: "run:D", type: "run", _x: 46, _y: 192},
  E: {id: "question:E", type: "question", _x: 296, _y: 192}
};
var WEDGES = [
  {from: "F", to: "A", kind: "about", provenance: ""},
  {from: "F", to: "C", kind: "about", provenance: ""},
  {from: "B", to: "D", kind: "related", provenance: ""},
  {from: "A", to: "D", kind: "related", provenance: ""},
  {from: "D", to: "E", kind: "related", provenance: ""}
];
''';

# (d) wide-focus golden: captured from the pre-change product by the
# /tmp throwaway (muse255-capture-wide-focus-golden.py), then frozen.
L10E_WIDE_FOCUS_GOLDEN = [
    '<line x1="450" y1="46" x2="46" y2="130" stroke="#8fb4e8" stroke-width="2"><title>about · </title></line>',
    '<line x1="450" y1="46" x2="546" y2="130" stroke="#8fb4e8" stroke-width="2"><title>about · </title></line>',
    '<line x1="296" y1="130" x2="46" y2="192" stroke="#88888855" stroke-width="1"><title>related · </title></line>',
    '<line x1="46" y1="130" x2="46" y2="192" stroke="#88888855" stroke-width="1"><title>related · </title></line>',
    '<line x1="46" y1="192" x2="296" y2="192" stroke="#88888855" stroke-width="1"><title>related · </title></line>',
]


def _l10e_script(checks, extra_stubs="", extra_decls=(), extra_fixtures=""):
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
    parts = [L10E_FRAG_STUBS + extra_stubs,
             "const nodeR = %s;" % m.group(1), mrow.group(0),
             _decl(src, "leftoverLaneD")]
    for name in ("incidentLaneMap", "relationLaneX", "relationLaneD",
                 "relationHintText", "laneScope") + tuple(extra_decls):
        parts.append(_decl(src, name))
    parts.append(L10E_GEO)
    parts.append("function __renderEdge(e, pos) {\n" + edge + "\n}")
    parts.append(L10E_FOCUS_POS + extra_fixtures)
    parts.append(checks)
    return "\n".join(parts)


def _l10e_wide_focus_outputs():
    """Render the wide-focus fixture through the REAL edge fragment
    with no gating (narrowTree=false, narrowCol=false). Used by the
    (d) golden test and by the pre-change capture throwaway."""
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
    script = (L10E_FRAG_STUBS + "\nconst nodeR = %s;" % m.group(1)
              + "\n" + mrow.group(0)
              + "\n" + _decl(src, "leftoverLaneD") + lane_d
              + "\n" + L10E_GEO
              + "\nfunction __renderEdge(e, pos) {\n" + edge + "\n}"
              + "\n" + L10E_WIDE_POS
              + '\ngraphSel = "F"; gFocus = "F"; narrowTree = false; narrowCol = false;'
                "\nvar OUTS = WEDGES.map(e => __renderEdge(e, WPOS));"
                "\nconsole.log(JSON.stringify(OUTS));"
                "\nconsole.log(JSON.stringify(throughNode(OUTS, WEDGES, WPOS)));")
    proc = _run_node(script)
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    return json.loads(lines[-2]), json.loads(lines[-1])


class L10eFocusLanesTest(unittest.TestCase):
    # (a) focus + q at narrow: the focused node's relations ride
    # distinct lanes; a selection inside focus narrows to that node;
    # zero through-node intersections either way.

    def test_focus_relations_use_distinct_clean_lanes(self):
        src = _script()
        for name in ("laneScope", "incidentLaneMap", "relationLaneX",
                     "relationLaneD", "relationHintText"):
            self.assertIn("function %s(" % name, src,
                          "missing L10e helper " + name)
        paint = _fn_body(src, "paintGraph")
        self.assertIn("if (narrowCol)", paint,
                      "edge gate must read the column scope")
        self.assertIn("laneScope(", paint,
                      "column scope must be computed in paint")
        proc = _run_node(_l10e_script(L10E_CHECKS_A))
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10e(a) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10eFocusQuerySemanticsTest(unittest.TestCase):
    # (b) q narrows within the focus neighborhood; the subtitle
    # states both scopes with exact counts.

    def test_query_narrows_within_neighborhood(self):
        src = _script()
        for name in ("focusQueryShow", "focusQuerySub"):
            self.assertIn("function %s(" % name, src,
                          "missing L10e helper " + name)
        paint = _fn_body(src, "paintGraph")
        self.assertIn("focusQueryShow(nodes, edges, gFocus, gQuery)", paint,
                      "focus filter must narrow by the query")
        self.assertIn("focusQuerySub(gLabel(center), fq.fullN, fq.hitN, "
                      "fq.centerHit, fq.ctxN, gQuery, fq.capped, graphTrunc)",
                      paint,
                      "subtitle must use honest match counts")
        self.assertLess(paint.index("focusQueryShow("),
                        paint.index("graphSearchPage(gQuery"),
                        "focus must filter before the search branch")
        self.assertIn("`${gLabel(center)} — 이웃 ${show.length - 1}건과 함께 보는 중`",
                      paint, "no-q focus subtitle must stay byte-identical")
        script = (L10E_FRAG_STUBS + "\n" + _decl(src, "graphSearchHits")
                  + "\n" + _decl(src, "focusQueryShow")
                  + "\n" + _decl(src, "focusQuerySub")
                  + "\n" + L10E_QNODES + "\n" + L10E_CHECKS_B)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10e(b) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10eHashRestoreTest(unittest.TestCase):
    # (c) a modeless hash after focus/group restores overview and
    # clears stale state; never focus with a null focus.

    def test_modeless_hash_restores_overview(self):
        src = _script()
        self.assertIn("function applyGraphHashState(", src,
                      "missing L10e helper applyGraphHashState")
        self.assertIn("applyGraphHashState(v)", src,
                      "hash handler must use the state helper")
        script = (L10E_HASH_STUBS + "\n" + _decl(src, "parseHash")
                  + "\n" + _decl(src, "clearGraphPanel")
                  + "\n" + _decl(src, "applyGraphHashState")
                  + "\n" + L10E_CHECKS_C)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10e(c) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10eWideFocusUnchangedTest(unittest.TestCase):
    # (d) the UNGATED fragment branch renders its byte-identical edge
    # output before/after. Since 257 this is NOT what product
    # wide-focus draws: the product gates relations and routes them
    # through row gaps plus the gutter (L10f-wide fixed the 45-crossing
    # hub). This golden pins the straight-line fallback branch only,
    # which narrowCol=false fixtures still exercise.

    def test_wide_focus_edge_output_byte_identical(self):
        self.assertTrue(L10E_WIDE_FOCUS_GOLDEN, "wide golden not captured")
        outs, bad = _l10e_wide_focus_outputs()
        self.assertEqual(outs, L10E_WIDE_FOCUS_GOLDEN)
        self.assertEqual(bad, [], "wide fixture grid stays through-node clean")


if __name__ == "__main__":
    unittest.main()
