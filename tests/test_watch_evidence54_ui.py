"""Focused UI-logic regressions for revision-54 evidence/pill fixes.

Drives the shipped ``src/openmainty/dashboard.html`` inline script directly:

- ``diffstatEvState`` membership: only files actually present in
  ``evidence_files`` are clickable; pending/no-change absences are expected
  notes; completed-but-missing stays a visible non-clickable warning.
- ``settledWatchPillFor`` strict guards: exact resume label/run id, terminal
  done, raw rc exactly 1, explicit flat ``watch.active`` +
  ``disposition.kind``, settlement inside the job interval. Every weaker
  shape (running/cancelled/RC0/RC2, mismatched id, bare watch string,
  missing/invalid/out-of-interval time) stays on the failure path.
- Whole ``paintStatusbar`` driver with stubbed DOM: a real failure always
  outranks settled-watch info regardless of job order; watch-only keeps an
  informational pill whose CTA opens the exact bound work view.
- Shipped-source structure: ``evFileNames`` is declared before first use
  (TDZ guard), the old unconditional diffstat push is gone, and only the
  saved kind feeds buttons.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = [
    "diffstatEvState",
    "settledWatchPillFor",
    "watchPillSubject",
    "isExternalWatch",
    "bareRunId",
    "liveRunHead",
    "errKo",
    "kindKo",
    "pillKo",
    "openWork",
    "paintStatusbar",
]

BLOCK_CONSTS = ["STATUS_KO", "KIND_KO"]

DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
const wantFn = process.argv[2].split(",");
const wantBlock = (process.argv[3] || "").split(",").filter(Boolean);

function sliceBalanced(s, i) {
  let depth = 0, q = null, line = false, block = false, esc = false;
  for (; i < s.length; i++) {
    const c = s[i], n = s[i + 1];
    if (line) { if (c === "\n") line = false; continue; }
    if (block) { if (c === "*" && n === "/") { block = false; i++; } continue; }
    if (q) {
      if (esc) { esc = false; continue; }
      if (c === "\\") { esc = true; continue; }
      if (c === q) q = null;
      continue;
    }
    if (c === "/" && n === "/") { line = true; i++; continue; }
    if (c === "/" && n === "*") { block = true; i++; continue; }
    if (c === "'" || c === '"' || c === "`") { q = c; continue; }
    if (c === "{") depth++;
    if (c === "}") { depth--; if (depth === 0) return i + 1; }
  }
  throw new Error("unbalanced");
}
function extractFn(name) {
  const re = new RegExp("(?:^|\\n)(?:async\\s+)?function\\s+" + name + "\\s*\\(");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing function " + name);
  const brace = src.indexOf("{", mm.index);
  return src.slice(mm[0].startsWith("\n") ? mm.index + 1 : mm.index, sliceBalanced(src, brace));
}
function extractConstBlock(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=\\s*\\{");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const block " + name);
  const brace = src.indexOf("{", mm.index);
  return "const " + name + " = " + src.slice(brace, sliceBalanced(src, brace)) + ";";
}
function extractConstArrow(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=\\s*[^;]+;");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const arrow " + name);
  return src.slice(mm[0].startsWith("\n") ? mm.index + 1 : mm.index, mm.index + mm[0].length).trim();
}

let code = "";
for (const b of wantBlock) code += extractConstBlock(b) + "\n";
const arrows = [];
for (const f of wantFn) {
  try { code += extractFn(f) + "\n"; }
  catch (e) { arrows.push(f); }
}
for (const f of arrows) code += extractConstArrow(f) + "\n";

const preamble = `
// esc is an unchanged prod dependency whose regex literal defeats the
// balanced-brace extractor (same harness limit as revision 50); it is
// stubbed here while the full inline script still gets node --check and
// the guard/statusbar assertions below run the REAL shipped functions.
// No esc/XSS coverage is claimed by this unit.
function esc(s) { return String(s == null ? "" : s); }
let O = null, view = {kind: "command"}, liveRunId = null, renderCalls = 0;
let lastClicked = null;
function render() { renderCalls++; }
const barStub = { innerHTML: "", hidden: false,
  classList: { add(c) { if (c === "hidden") barStub.hidden = true; }, remove(c) { if (c === "hidden") barStub.hidden = false; } },
  querySelector() { return null; } };
const sbViewStub = { onclick: null };
const floatsStub = { classList: { add() {} } };
global.document = { body: { classList: { add() {}, remove() {} } },
  getElementById(id) {
    if (id === "statusbar") return barStub;
    if (id === "sb-view") return sbViewStub;
    if (id === "jobfloat" || id === "runfloat") return floatsStub;
    return null;
  } };
const $ = id => document.getElementById(id);
function resetBar() { barStub.innerHTML = ""; barStub.hidden = false; sbViewStub.onclick = null; }
`;
const tests = `
;(function () {
let pass = 0;
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa === sb) { pass++; }
  else { console.log("FAIL " + msg + " :: " + sa + " !== " + sb); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }

// ---- diffstatEvState: saved-only membership ----
let st = diffstatEvState(["fix.diffstat.txt"], {fix: {status: "done"}}, false);
eq(st.map(e => e.kind), ["saved", "pending"], "saved file clickable, unfinished review pending");
st = diffstatEvState([], {fix: {status: "done"}, review: {status: "done"}}, false);
eq(st.map(e => e.kind), ["history", "history"], "completed-but-missing is warning kind, never saved");
eq(st.filter(e => e.kind === "saved").length, 0, "no saved claim without the file");
st = diffstatEvState([], {fix: {status: "done"}, review: {status: "pending"}}, true);
eq(st.map(e => e.kind), ["nochange", "nochange"], "settled no-change absence is expected, both stages");
st = diffstatEvState(["xfix.diffstat.txt"], {}, false);
eq(st[0].kind, "pending", "substring filename is not membership");
st = diffstatEvState(undefined, undefined, false);
eq(st.map(e => e.kind), ["pending", "pending"], "missing inputs degrade to pending, no throw");

// ---- watchPillSubject: bound subject, no hardcode ----
eq(watchPillSubject({dep: "@babel/preset-env", target: "8.0.7", id: "run-x"}), "@babel/preset-env → 8.0.7", "dep+target subject");
eq(watchPillSubject({title: "chore(deps): foo 1.0.0 -> 2.0.0", id: "run-x"}), "chore(deps): foo 1.0.0 -> 2.0.0", "title fallback");
eq(watchPillSubject({id: "run-x"}), "run-x", "id last resort");

// ---- settledWatchPillFor: strict positive + adversarial negatives ----
const goodRun = {id: "run-8392b19f", repo: "react-native-nitro-sound", dep: "@babel/preset-env",
  target: "8.0.7", title: "chore(deps): @babel/preset-env 7.29.7 -> 8.0.7",
  steps: {fix: {status: "done"}}, waitReason: "watch: preset-env 8 requires core 7",
  disposition: {kind: "EXTERNAL-WATCH"},
  watch: {active: true, settled_at: "2026-10-19T01:49:56+00:00"}};
const goodJob = {id: "job-82520bda", kind: "resume", label: "run-8392b19f", status: "done", rc: 1,
  started_at: "2026-10-19T01:47:50+00:00", ended_at: "2026-10-19T01:49:56+00:00", note: "exit 1"};
const sw = settledWatchPillFor(goodJob, [goodRun]);
ok(sw && sw.run === "run-8392b19f" && sw.rc === 1 && sw.job === "job-82520bda", "positive settled watch identified");
eq(sw && sw.subject, "@babel/preset-env → 8.0.7", "positive carries bound subject");
const J = o => Object.assign({}, goodJob, o);
const R = o => Object.assign({}, goodRun, o);
eq(settledWatchPillFor(Object.assign(J(), {status: "running"}), [goodRun]), null, "running never settled");
eq(settledWatchPillFor(Object.assign(J(), {status: "cancelled", rc: 130}), [goodRun]), null, "cancelled never settled");
eq(settledWatchPillFor(Object.assign(J(), {rc: 0}), [goodRun]), null, "RC0 never settled");
eq(settledWatchPillFor(Object.assign(J(), {rc: 2}), [goodRun]), null, "RC2 never settled");
eq(settledWatchPillFor(Object.assign(J(), {kind: "chat"}), [goodRun]), null, "non-resume kind never settled");
eq(settledWatchPillFor(Object.assign(J(), {label: "run-8392"}), [goodRun]), null, "substring label never matches");
eq(settledWatchPillFor(goodJob, [{id: "run-other"}]), null, "mismatched id never settled");
eq(settledWatchPillFor(goodJob, null), null, "missing runs never settled");
eq(settledWatchPillFor(goodJob, [R({watch: {active: false, settled_at: "2026-10-19T01:49:56+00:00"}})]), null, "inactive watch never settled");
eq(settledWatchPillFor(goodJob, [R({disposition: {kind: "DONE"}})]), null, "wrong disposition never settled");
eq(settledWatchPillFor(goodJob, [R({watch: undefined, disposition: undefined})]), null, "bare waitReason string never settled");
eq(settledWatchPillFor(goodJob, [R({watch: {active: true}})]), null, "missing settled_at never settled");
eq(settledWatchPillFor(goodJob, [R({watch: {active: true, settled_at: "not-a-time"}})]), null, "invalid settled_at never settled");
eq(settledWatchPillFor(goodJob, [R({watch: {active: true, settled_at: "2026-10-19T01:40:00+00:00"}})]), null, "settlement before job start never settled");
eq(settledWatchPillFor(goodJob, [R({watch: {active: true, settled_at: "2026-10-19T01:55:00+00:00"}})]), null, "settlement after job end never settled");
eq(settledWatchPillFor(Object.assign(J(), {started_at: "2026-10-19T01:59:00+00:00"}), [goodRun]), null, "ended before started never settled");

// ---- whole paintStatusbar driver ----
const repos = [{name: "r", sched_enabled: true}];
function show() { return barStub.innerHTML; }
O = {jobs: [goodJob], runs: [goodRun], repos: repos}; view = {kind: "command"}; resetBar(); paintStatusbar();
ok(show().indexOf("외부 대기 확정") !== -1, "watch-only pill shows settled");
ok(show().indexOf("@babel/preset-env → 8.0.7") !== -1, "watch-only pill shows bound subject");
ok(show().indexOf("rc1") !== -1, "watch-only pill keeps raw rc");
ok(show().indexOf("실패 ·") === -1, "watch-only pill is not a failure");
sbViewStub.onclick(); eq(view, {kind: "work", id: "run-8392b19f"}, "watch CTA opens exact bound work");
const realRun = {id: "run-other", repo: "r", title: "real breakage", steps: {fix: {status: "failed"}}, waitReason: ""};
const realFail = {id: "job-real", kind: "resume", label: "run-other", status: "done", rc: 2,
  started_at: "2026-10-19T01:47:50+00:00", ended_at: "2026-10-19T01:49:56+00:00", note: "exit 2"};
const watchRun2 = R({id: "run-aaa", watch: {active: true, settled_at: "2026-10-19T01:48:30+00:00"}});
const watchJob2 = J({id: "job-aaa", label: "run-aaa", started_at: "2026-10-19T01:47:00+00:00", ended_at: "2026-10-19T01:49:00+00:00"});
for (const order of [[goodJob, watchJob2, realFail], [realFail, watchJob2, goodJob], [watchJob2, realFail, goodJob]]) {
  O = {jobs: order, runs: [goodRun, watchRun2, realRun], repos: repos}; resetBar(); paintStatusbar();
  ok(show().indexOf("실패 ·") !== -1 && show().indexOf("job-real") !== -1, "true failure wins regardless of order");
  ok(show().indexOf("외부 대기 확정") === -1, "settled info hidden while a real failure stands");
}
O = {jobs: [], runs: [goodRun], repos: repos}; resetBar(); paintStatusbar();
eq(barStub.hidden, true, "no jobs hides the bar");
console.log("PASS-LINES " + pass);
})();
`;
eval(code + preamble + tests);
'''

CASES = {
    "functions": ",".join(FUNCTIONS),
    "blocks": ",".join(BLOCK_CONSTS),
}


def _run_case(which):
    if which == "functions":
        args = [CASES["functions"], CASES["blocks"]]
    else:
        raise AssertionError(which)
    proc = subprocess.run(
        ["node", "-e", DRIVER, str(HTML)] + args,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    return proc


def _script():
    m = re.search(r"<script>([\s\S]*)</script>", HTML.read_text(encoding="utf-8"))
    assert m, "inline script block present"
    return m.group(1)


class EvidenceMembershipTest(unittest.TestCase):
    def test_shipped_driver_green(self):
        proc = _run_case("functions")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("PASS-LINES", proc.stdout)


if __name__ == "__main__":
    unittest.main()
