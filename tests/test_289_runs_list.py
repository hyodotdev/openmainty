"""Slice 289, STAGED: make #/runs usable with many runs.

Failing-first pins for the runs list rework (all in dashboard.html):
state filter chips with counts (aria-pressed, 44px), a repo filter, a
latest-only toggle, 30-row paging with a "30건 더 보기" button, filter
state in the hash query, a readable stage summary for text/AT, compact
cards at narrow widths, and an empty state with "필터 지우기".
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
function pillKo(s) { return "<i>" + s + "</i>"; }
function kindKo(k) { return k; }
function waitKo(w) { return w || ""; }
function settledWatchPillFor() { return null; }
function focusViewHeading() {}
function detail() {}
function openWork() {}
function cancelJob() {}
var CSS = {escape: function(s) { return String(s); }};
var selRun = null;
var runsShown = 30, runsShownSig = "", runsFocusKey = null, runsLastIds = [];
var boxHtml = "";
var document = {querySelectorAll: function() { return []; }};
function $(id) {
  if (id === "view") {
    return {set innerHTML(h) { boxHtml = h; },
            get innerHTML() { return boxHtml; }};
  }
  if (id === "bartitle") return {textContent: ""};
  return {textContent: ""};
}
var location = {hash: "#/runs"};
var history = {replaceState: function(h, t, u) { location.hash = u; }};
/* 35-run fixture: 2 repos, mixed states, issues 0-5 per repo.
   i%7==0 question (also a blocked step), i%7==1 blocked,
   i%7==2 running, else all-done. created_at rises with i. */
function mkRuns() {
  var runs = [];
  for (var i = 0; i < 35; i++) {
    var steps;
    var wait = "";
    if (i % 7 === 0) {
      wait = "question: Q" + i;
      steps = {triage: {status: "done"},
               analyze: {status: "blocked"},
               fix: {status: "pending"}};
    } else if (i % 7 === 1) {
      steps = {triage: {status: "done"},
               analyze: {status: "failed"},
               fix: {status: "pending"}};
    } else if (i % 7 === 2) {
      steps = {triage: {status: "done"},
               analyze: {status: "running"},
               fix: {status: "pending"}};
    } else {
      steps = {triage: {status: "done"},
               analyze: {status: "done"},
               fix: {status: "done"}};
    }
    runs.push({id: "run-" + (i < 10 ? "0" + i : i),
               repo: i % 2 === 0 ? "repo-a" : "repo-b",
               issue: String(Math.floor(i / 6)), title: "t" + i,
               waitReason: wait, steps: steps,
               created_at: "2026-11-04T10:00:"
                 + (i < 10 ? "0" + i : i) + "Z"});
  }
  return runs;
}
function rowIds(html) {
  var ids = [], re = /data-runrow="([^"]+)"/g, m;
  while ((m = re.exec(html)) !== null) ids.push(m[1]);
  return ids;
}
'''

RUNS_FUNCS = ["esc", "latestPerIssue", "runStageSummary",
              "runsMatchState", "runsCountByState", "runsRepos",
              "runsFilter", "runsFilterSig", "runsViewFilter",
              "runsChipsHtml", "runsRepoSelHtml", "runsCountHtml",
              "runsRowHtml", "runsCardHtml", "runsNarrow",
              "runsSetFilter", "runsClearFilter", "runsMore",
              "runsApplyFocus", "renderRuns"]


def _prog(src, body, extra=()):
    decls = "".join(_decl(src, name) + "\n" for name in RUNS_FUNCS)
    for name in extra:
        decls += _decl(src, name) + "\n"
    return STUBS + _const_line(src, "RUNS_PAGE") + "\n" + decls + body


class RunsListSourcePins(unittest.TestCase):
    def test_helpers_exist_and_page_is_30(self):
        src = _script()
        for name in RUNS_FUNCS:
            _decl(src, name)
        self.assertIn("const RUNS_PAGE = 30;", src)

    def test_more_and_clear_labels(self):
        src = _script()
        self.assertIn("30건 더 보기", src)
        self.assertIn("필터 지우기", src)
        self.assertNotIn("더 보기 →", src)

    def test_chips_are_pressed_buttons_with_polite_count(self):
        src = _script()
        chips = _decl(src, "runsChipsHtml")
        self.assertIn("aria-pressed", chips)
        self.assertIn("<button", chips)
        self.assertIn("data-fk=", chips)
        count = _decl(src, "runsCountHtml")
        self.assertIn('role="status"', count)
        self.assertIn("건 중", count)
        self.assertIn("모두 표시", count)

    def test_hash_query_round_trip_shape(self):
        src = _script()
        self.assertRegex(src, r'if \(seg\[0\] === "runs"\) return '
                             r'\{kind: "runs", state: q\.state')
        self.assertIn("latest=", _decl(src, "syncHash"))

    def test_quiet_poll_keeps_runs_focus_by_key(self):
        src = _script()
        self.assertIn('view.kind === "runs"', _decl(src, "captureFk"))
        self.assertIn('view.kind === "runs"', _decl(src, "restoreFk"))

    def test_narrow_breakpoint_rerenders_runs(self):
        src = _script()
        self.assertIn("runsNarrow", _decl(src, "renderRuns"))
        self.assertIn('(max-width: 480px)', src)
        self.assertIn("runsMqGo", src)

    def test_stage_cell_has_text_summary_and_keeps_chips(self):
        src = _script()
        row = _decl(src, "runsRowHtml")
        self.assertIn("runStageSummary", row)
        self.assertIn("aria-hidden", row)
        self.assertIn('class="vh"', row)
        self.assertIn("pillKo", row)


class RunsListBehavior(unittest.TestCase):
    def test_chips_counts_and_pressed(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs"};
renderRuns();
ok(boxHtml.indexOf("전체") !== -1, "all-chip");
ok(/질문 있음[\s\S]{0,40}>5</.test(boxHtml), "q-count");
ok(/막힘[\s\S]{0,40}>10</.test(boxHtml), "blocked-count");
ok(/진행 중[\s\S]{0,40}>5</.test(boxHtml), "running-count");
ok(/완료[\s\S]{0,40}>20</.test(boxHtml), "done-count");
var pressed = (boxHtml.match(/aria-pressed="true"/g) || []).length;
ok(pressed === 1, "one-pressed:" + pressed);
ok(/data-fk="runs-state-all"[^>]*aria-pressed="true"|aria-pressed="true"[^>]*data-fk="runs-state-all"/.test(boxHtml),
   "all-pressed");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_default_page_is_30_then_more_shows_all(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs"};
renderRuns();
ok(rowIds(boxHtml).length === 30, "first-30:" + rowIds(boxHtml).length);
ok(boxHtml.indexOf("30건 더 보기") !== -1, "more-present");
ok(boxHtml.indexOf("35건 중 30건 표시") !== -1, "count-line");
runsMore();
ok(rowIds(boxHtml).length === 35, "all-35:" + rowIds(boxHtml).length);
ok(boxHtml.indexOf("30건 더 보기") === -1, "more-gone");
ok(boxHtml.indexOf("35건 모두 표시") !== -1, "count-all");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_state_repo_latest_filters_combine(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs", state: "question", repo: "", latest: ""};
renderRuns();
ok(rowIds(boxHtml).length === 5, "q5:" + rowIds(boxHtml).length);
ok(boxHtml.indexOf("30건 더 보기") === -1, "q-no-more");
view = {kind: "runs", state: "", repo: "repo-b", latest: ""};
runsShownSig = ""; renderRuns();
ok(rowIds(boxHtml).length === 17, "repob17:" + rowIds(boxHtml).length);
ok(boxHtml.indexOf("전체") !== -1, "chips-kept");
view = {kind: "runs", state: "", repo: "", latest: "1"};
runsShownSig = ""; renderRuns();
ok(rowIds(boxHtml).length === 12, "latest12:" + rowIds(boxHtml).length);
view = {kind: "runs", state: "done", repo: "repo-a", latest: "1"};
runsShownSig = ""; renderRuns();
var ids = rowIds(boxHtml);
ok(ids.length === 3, "combo3:" + ids.join(","));
ok(ids.join() === "run-04,run-10,run-34", "combo-ids:" + ids.join(","));
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_empty_filter_result_offers_clear(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs", state: "question", repo: "no-such-repo",
            latest: ""};
renderRuns();
ok(rowIds(boxHtml).length === 0, "no-rows");
ok(boxHtml.indexOf("필터 지우기") !== -1, "clear-present");
ok(boxHtml.indexOf("data-work=") === -1, "no-detail-links");
ok(boxHtml.indexOf("runs-state-all") !== -1, "chips-kept");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_stage_summary_text(self):
        src = _script()
        prog = _prog(src, r'''
ok(runStageSummary({a: {status: "done"}, b: {status: "done"},
                    c: {status: "running"}, d: {status: "failed"},
                    e: {status: "pending"}, f: {status: "pending"},
                    g: {status: "pending"}, h: {status: "pending"}})
   === "8단계 중 완료 2건, 진행 중 1건, 차단 1건, 대기 4건", "mixed");
ok(runStageSummary({a: {status: "done"}, b: {status: "done"},
                    c: {status: "done"}}) === "3단계 중 완료 3건", "all-done");
ok(runStageSummary({a: {status: "blocked"}}) === "1단계 중 차단 1건",
   "blocked-only");
ok(runStageSummary({}) === "단계 없음", "empty");
ok(runStageSummary(null) === "단계 없음", "null");
ok(runStageSummary({a: {status: "weird"}}) === "1단계 중 기타 1건",
   "unknown");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_row_and_card_shapes(self):
        src = _script()
        prog = _prog(src, r'''
var s = {id: "run-07", repo: "repo-a", issue: "1", title: "t7",
         waitReason: "question: Q7",
         steps: {triage: {status: "done"},
                 analyze: {status: "blocked"}}};
var row = runsRowHtml(s, true);
ok(row.indexOf('aria-label="run-07 상세"') !== -1, "row-named");
ok(row.indexOf(">상세</a>") !== -1, "row-visible");
ok(row.indexOf("최신") !== -1, "row-latest");
ok(row.indexOf("2단계 중 완료 1건, 차단 1건") !== -1, "row-summary");
ok(row.indexOf("data-runrow=") !== -1, "row-key");
var card = runsCardHtml(s, true);
ok(card.indexOf("run-07") !== -1, "card-id");
ok(card.indexOf("repo-a#1 t7") !== -1, "card-title");
ok(card.indexOf("2단계 중 완료 1건, 차단 1건") !== -1, "card-summary");
ok(card.indexOf("질문 있음") !== -1, "card-qbadge");
ok(card.indexOf("최신") !== -1, "card-latest");
ok(card.indexOf('aria-label="run-07 상세"') !== -1, "card-named");
var plain = runsCardHtml({id: "r", repo: "x", issue: "2", title: "t",
                          waitReason: "", steps: {}}, false);
ok(plain.indexOf("질문 있음") === -1, "card-no-qbadge");
ok(plain.indexOf("최신") === -1, "card-no-latest");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_narrow_renders_cards_not_table(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs"};
var window = {matchMedia: function() { return {matches: true}; }};
renderRuns();
ok(boxHtml.indexOf("<table") === -1, "no-table");
ok(boxHtml.indexOf("runcards") !== -1, "cards");
ok(rowIds(boxHtml).length === 30, "cards-paged");
window = {matchMedia: function() { return {matches: false}; }};
runsShownSig = ""; renderRuns();
ok(boxHtml.indexOf("<table") !== -1, "table-back");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_hash_round_trip(self):
        src = _script()
        prog = (STUBS + _decl(src, "parseHash") + _decl(src, "syncHash")
                + r'''
var view = {kind: "command"};
location.hash = "#/runs?state=blocked&repo=repo-a&latest=1";
var v = parseHash();
ok(v.kind === "runs" && v.state === "blocked" && v.repo === "repo-a"
   && v.latest === "1", "parsed:" + JSON.stringify(v));
view = v; syncHash();
ok(location.hash === "#/runs?state=blocked&repo=repo-a&latest=1",
   "synced:" + location.hash);
location.hash = "#/runs";
v = parseHash();
ok(v.kind === "runs" && v.state === "" && v.repo === ""
   && v.latest === "", "bare:" + JSON.stringify(v));
view = v; syncHash();
ok(location.hash === "#/runs", "bare-synced:" + location.hash);
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_filter_clicks_rewrite_hash_and_arm_focus(self):
        src = _script()
        prog = _prog(src, r'''
var view = {kind: "runs", state: "", repo: "", latest: ""};
location.hash = "#/runs";
runsSetFilter("state", "done", "runs-state-done");
ok(location.hash === "#/runs?state=done", "hash:" + location.hash);
ok(runsFocusKey === "runs-state-done", "focus-armed");
view = {kind: "runs", state: "done", repo: "", latest: ""};
runsSetFilter("latest", "1", "runs-latest");
ok(location.hash === "#/runs?state=done&latest=1", "merged:" + location.hash);
runsClearFilter();
ok(location.hash === "#/runs", "cleared:" + location.hash);
ok(runsFocusKey === "runs-state-all", "clear-focus");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_render_restores_armed_focus_then_clears(self):
        src = _script()
        prog = _prog(src, r'''
var focused = [];
function mkEl(name) {
  return {focus: function() { focused.push(name); }};
}
var viewRoot = {
  innerHTML: "",
  querySelector: function(sel) {
    if (sel.indexOf("runs-state-done") !== -1) return mkEl("chip-done");
    if (sel.indexOf("run-33") !== -1) return mkEl("row-33");
    return null;
  }
};
$ = function(id) {
  if (id === "view") {
    return {set innerHTML(h) { boxHtml = h; viewRoot.innerHTML = h; },
            get innerHTML() { return boxHtml; },
            querySelector: viewRoot.querySelector};
  }
  return {textContent: ""};
};
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs"};
runsFocusKey = "runs-state-done";
renderRuns();
ok(focused.join() === "chip-done", "chip:" + focused.join());
ok(runsFocusKey === null, "cleared");
runsFocusKey = "row:run-33";
renderRuns();
ok(focused.join() === "chip-done,row-33", "row:" + focused.join());
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_quiet_poll_restores_runs_focus_by_key(self):
        src = _script()
        prog = (STUBS + _decl(src, "runsFilterSig")
                + _decl(src, "captureFk") + _decl(src, "restoreFk") + r'''
var chip = {dataset: {fk: "runs-state-q"},
            focus: function() { document.activeElement = chip; }};
var viewRoot = {
  contains: function(el) { return el === chip; },
  querySelector: function(sel) { return chip; }
};
$ = function(id) {
  if (id === "view") return viewRoot;
  return {textContent: ""};
};
var view = {kind: "runs", state: "question", repo: "", latest: ""};
document.activeElement = chip;
var kept = captureFk();
ok(kept && kept.kind === "runs" && kept.key === "runs-state-q",
   "captured:" + JSON.stringify(kept));
document.activeElement = null;
ok(restoreFk(kept) === true, "restored");
ok(document.activeElement === chip, "active");
view = {kind: "runs", state: "done", repo: "", latest: ""};
ok(restoreFk(kept) === false, "filter-change-no-restore");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_paging_resets_on_filter_change(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs"};
runsShown = 60; runsShownSig = "stale";
renderRuns();
ok(rowIds(boxHtml).length === 30, "reset-30");
ok(runsShown === 30, "shown-reset");
renderRuns();
ok(rowIds(boxHtml).length === 30, "stable-30");
runsMore();
ok(rowIds(boxHtml).length === 35, "more-35");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_unknown_state_shows_all(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "", runs: mkRuns()};
var view = {kind: "runs", state: "bogus", repo: "", latest: ""};
renderRuns();
ok(rowIds(boxHtml).length === 30, "all-paged");
ok(/data-fk="runs-state-all"[^>]*aria-pressed="true"|aria-pressed="true"[^>]*data-fk="runs-state-all"/.test(boxHtml),
   "all-pressed");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_repo_names_are_escaped(self):
        src = _script()
        prog = _prog(src, r'''
var O = {jobs: [], watch_log_tail: "",
         runs: [{id: "run-x", repo: "r<b>", issue: "1", title: "t",
                 waitReason: "", steps: {a: {status: "done"}},
                 created_at: "2026-11-04T10:00:00Z"}]};
var view = {kind: "runs"};
renderRuns();
ok(boxHtml.indexOf("r<b>") === -1, "no-raw");
ok(boxHtml.indexOf("r&lt;b&gt;") !== -1, "escaped");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)


class RunsListPolish289b(unittest.TestCase):
    """289b: chip count separation (F1) and repo select styling (F2)."""

    def test_f1_chip_count_gap(self):
        src = _script()
        m = re.search(r"\.runs-chips \.btn \{([^}]*)\}", src)
        self.assertIsNotNone(m, "runs-chips .btn rule")
        self.assertIn("gap:", m.group(1))

    def test_f1_accessible_name_keeps_space(self):
        chips = _decl(_script(), "runsChipsHtml")
        self.assertIn('} <span class="tnum">', chips)

    def test_f2_repo_select_styled(self):
        src = _script()
        m = re.search(r"\.runs-filter select \{([^}]*)\}", src)
        self.assertIsNotNone(m, "runs-filter select rule")
        body = m.group(1)
        self.assertIn("min-height: 44px", body)
        self.assertIn("border-radius: var(--r-md)", body)
        self.assertIn("border: 1px solid var(--hair2)", body)
        self.assertIn("background: none", body)
        self.assertIn("color: inherit", body)


if __name__ == "__main__":
    unittest.main()
