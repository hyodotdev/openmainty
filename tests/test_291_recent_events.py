"""Slice 291, STAGED: group #/command recent events.

Failing-first pins for the recent-events rework (dashboard.html
only): each repo's own run of same-kind/identical-title events
folds into one row with '같은 결과 N회' and a time span (other
repos interleaved between do not break the run), differing events
stay separate, at most 6 rows show first with an 'N건 더 보기'
button, and the empty state is kept.
"""
import re
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


def _const_line(src, name):
    m = re.search(r"(?:const|let|var)\s+" + re.escape(name)
                  + r"\s*=\s*[^;]+;", src)
    if not m:
        raise AssertionError("missing const " + name)
    return m.group(0)


def _run_node(prog):
    return subprocess.run(
        [NODE, "-e", prog], capture_output=True, text=True, timeout=60)


STUBS = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
var O = {repos: []};
var eventsCache = [];
var evShown = 6, evShownSig = "", evFocusRow = null;
var focused = null;
var boxEl = {innerHTML: "",
  querySelector: function (sel) {
    var m = /data-evrow="(\d+)"/.exec(sel);
    if (!m) return null;
    return {focus: function () { focused = Number(m[1]); }};
  }};
function $(id) {
  if (id === "evlist") return boxEl;
  if (id === "ev-more") {
    if (boxEl.innerHTML.indexOf('id="ev-more"') < 0) return null;
    return {set onclick(f) { boxEl._more = f; }, get onclick() { return boxEl._more; }};
  }
  return null;
}
function mkEv(at, repo, kind, title, detail) {
  return {at: at, repo: repo, kind: kind, ref: "", title: title,
          detail: detail || "", links: [], actor: "openmainty"};
}
'''

FUNCS = ["groupEvents", "evSpanHtml", "eventGroupHtml",
         "eventHtml", "eventDetailHtml", "fmtKST", "fmtKSThm",
         "fmtKSTtime", "fmtKSTdayKey", "paintEvlist", "evMore",
         "ghIssueUrl", "esc"]
CONSTS = ["evKo", "EV_KO", "EV_ROWS_FIRST"]


def _prog(src, body):
    decls = "\n".join(_decl(src, n) for n in FUNCS)
    consts = "\n".join(_const_line(src, n) for n in CONSTS)
    return STUBS + "\n" + decls + "\n" + consts + "\n" + body


def _check(testcase, src, body):
    r = _run_node(_prog(src, body))
    testcase.assertEqual(
        r.returncode, 0,
        "node driver failed:\n" + (r.stdout or "") + (r.stderr or ""))


class RecentEvents291Case(unittest.TestCase):
    def test_groups_identical_consecutive(self):
        src = _script()
        _check(self, src, r'''
var evs = [
  mkEv("2026-11-04T05:47:00Z", "r1", "scan", "scan: 11 deps", "d1"),
  mkEv("2026-11-04T05:12:00Z", "r1", "scan", "scan: 11 deps", "d2"),
  mkEv("2026-11-04T04:37:00Z", "r1", "scan", "scan: 11 deps", "d3"),
  mkEv("2026-11-04T04:02:00Z", "r1", "scan", "scan: 12 deps", "d4")
];
var g = groupEvents(evs);
ok(g.length === 2, "two groups, got " + g.length);
ok(g[0].items.length === 3, "first group holds 3");
ok(g[1].items.length === 1, "second group holds 1");
var total = g.reduce(function (n, x) { return n + x.items.length; }, 0);
ok(total === evs.length, "count equals events, no drop");
ok(g[0].items[0].detail === "d1", "newest first in group");
''')

    def test_differing_events_not_merged(self):
        src = _script()
        _check(self, src, r'''
var evs = [
  mkEv("2026-11-04T05:47:00Z", "r1", "scan", "same", ""),
  mkEv("2026-11-04T05:12:00Z", "r2", "scan", "same", ""),
  mkEv("2026-11-04T04:37:00Z", "r1", "error", "same", "")
];
var g = groupEvents(evs);
ok(g.length === 3, "repo/kind breaks group, got " + g.length);
var gap = [
  mkEv("2026-11-04T05:47:00Z", "r1", "scan", "A", ""),
  mkEv("2026-11-04T05:12:00Z", "r1", "scan", "B", ""),
  mkEv("2026-11-04T04:37:00Z", "r1", "scan", "A", "")
];
ok(groupEvents(gap).length === 3, "same-repo gap splits run");
''')

    def test_interleaved_repos_group_per_repo(self):
        src = _script()
        _check(self, src, r'''
var evs = [];
for (var i = 0; i < 15; i++) {
  var repo = (i % 2 === 0) ? "cal" : "nitro";
  var title = (repo === "cal") ? "scan: 11 deps" : "scan: 68 deps";
  evs.push(mkEv("2026-11-04T" + String(10 + i).slice(-2) + ":00:00Z",
                repo, "scan", title, ""));
}
var g = groupEvents(evs);
ok(g.length === 2, "two per-repo runs, got " + g.length);
ok(g[0].items.length === 8, "cal holds 8, got " + g[0].items.length);
ok(g[1].items.length === 7, "nitro holds 7, got " + g[1].items.length);
var total = g.reduce(function (n, x) { return n + x.items.length; }, 0);
ok(total === 15, "count equals events, no drop");
''')

    def test_same_repo_title_change_splits_run(self):
        src = _script()
        _check(self, src, r'''
var evs = [
  mkEv("2026-11-04T05:00:00Z", "r1", "scan", "X", ""),
  mkEv("2026-11-04T04:00:00Z", "r2", "scan", "X", ""),
  mkEv("2026-11-04T03:00:00Z", "r1", "scan", "Y", ""),
  mkEv("2026-11-04T02:00:00Z", "r2", "scan", "X", ""),
  mkEv("2026-11-04T01:00:00Z", "r1", "scan", "X", "")
];
var g = groupEvents(evs);
ok(g.length === 4, "r1 run splits on Y, got " + g.length);
ok(g[0].items.length === 1 && g[3].items.length === 1,
   "r1 X runs stay apart");
ok(g[1].items.length === 2, "r2 X run joins across repos");
''')

    def test_group_row_reads_one_sentence(self):
        src = _script()
        _check(self, src, r'''
var evs = [
  mkEv("2026-11-04T05:47:00Z", "r1", "scan", "scan: 11 deps", "new-detail"),
  mkEv("2026-11-04T01:12:00Z", "r1", "scan", "scan: 11 deps", "old-detail")
];
var h = eventGroupHtml({items: evs}, 0);
ok(h.indexOf("같은 결과 2회") >= 0, "count shown");
ok(h.indexOf("마지막") >= 0 && h.indexOf("처음") >= 0, "span shown");
ok(h.indexOf("new-detail") >= 0, "newest detail kept");
ok(h.indexOf("old-detail") < 0, "older detail not duplicated");
ok(h.indexOf("상세 보기 (마지막 사건)") >= 0, "toggle names newest");
ok(h.indexOf("scan: 11 deps") >= 0, "title kept");
var single = eventGroupHtml({items: [evs[0]]}, 0);
ok(single.indexOf("같은 결과") < 0, "single has no count");
ok(single.indexOf("data-evrow=\"0\"") >= 0, "row key for focus");
''')

    def test_six_rows_then_more_button(self):
        src = _script()
        _check(self, src, r'''
ok(EV_ROWS_FIRST === 6, "page is 6");
for (var i = 0; i < 8; i++)
  eventsCache.push(mkEv("2026-11-04T0" + i + ":00:00Z", "r1", "scan",
                        "title " + i, ""));
paintEvlist();
var rows = (boxEl.innerHTML.match(/data-evrow="/g) || []).length;
ok(rows === 6, "6 rows first, got " + rows);
ok(boxEl.innerHTML.indexOf("2건 더 보기") >= 0, "more names rest");
ok(typeof boxEl._more === "function", "more wired");
boxEl._more();
rows = (boxEl.innerHTML.match(/data-evrow="/g) || []).length;
ok(rows === 8, "all rows after more, got " + rows);
ok(boxEl.innerHTML.indexOf("더 보기") < 0, "button gone when all fit");
ok(focused === 6, "focus on first new row, got " + focused);
''')

    def test_empty_state_kept(self):
        src = _script()
        _check(self, src, r'''
eventsCache = [];
paintEvlist();
ok(boxEl.innerHTML.indexOf("아직 사건 없음 — 스캔 후에 기록됩니다.") >= 0,
   "empty state kept");
''')

    def test_detail_links_kept(self):
        src = _script()
        _check(self, src, r'''
var evs = [mkEv("2026-11-04T05:47:00Z", "r1", "scan", "t",
                "a->1; b->2; c->3; d->4")];
var h = eventGroupHtml({items: evs}, 0);
ok(h.indexOf("의존성에서 보기") >= 0, "deps link kept");
ok(h.indexOf("inline-err") < 0, "no error style");
''')
