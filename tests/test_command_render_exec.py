"""Real-execution regression for the command-center render path.

Extracts the actual ``renderCommand`` (plus its real dependencies) from
the shipped ``src/openmainty/dashboard.html`` inline script and executes
it in node against a minimal fake DOM with one repo, derived review
counts, and raw queue history. A dangling variable (e.g. the removed
``statline``) throws ReferenceError here instead of in Chrome, and the
test asserts the produced heading, derived projection, and history
disclosure — not string presence in source.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

LINE_FUNCTIONS = ["esc"]

FUNCTIONS = [
    "renderCommand",
    "latestPerIssue",
    "isSupersededItem",
    "supersededNoteIsNoChange",
    "bareRunId",
    "homeRunHead",
    "ghIssueUrl",
    "qc",
    "errKo",
    "fmtKST",
    "fmtKSThm",
    "ago",
    "workerLockWord",
    "workerHeadline",
    "isNoCodeDone",
    "isValidatedNoChange",
    "runOutcomeWord",
    "isExternalWatch",
    "nextLabel",
    "nextProv",
    "composerBusy",
    "driftKo",
    "eventHtml",
    "eventDetailHtml",
    "groupEvents",
    "evSpanHtml",
    "eventGroupHtml",
    "paintEvlist",
    "evMore",
    "fmtKSTtime",
    "fmtKSTdayKey",
]
BLOCK_CONSTS = ["EV_KO"]
CONST_LINES = ["evKo", "EV_ROWS_FIRST"]

DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
const wantFn = process.argv[2].split(",");
const wantBlock = process.argv[3].split(",");
const wantLine = process.argv[4].split(",").filter(Boolean);
const wantConst = (process.argv[5] || "").split(",").filter(Boolean);
function extractFnLine(name) {
  const re = new RegExp("(?:^|\\n)((?:async\\s+)?function\\s+" + name + "\\s*\\([^\\n]*?\\{[^\\n]*\\})");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing single-line function " + name);
  return mm[1];
}
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
  const mm = src.match(new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=\\s*\\{"));
  if (!mm) throw new Error("missing const block " + name);
  const brace = src.indexOf("{", mm.index);
  return "const " + name + " = " + src.slice(brace, sliceBalanced(src, brace)) + ";";
}
function makeEl(id) {
  return {id: id, innerHTML: "", textContent: "", value: "", dataset: {},
    style: {}, hidden: false,
    classList: {add: function () {}, remove: function () {}, toggle: function () {}},
    setSelectionRange: function () {}, focus: function () {},
    addEventListener: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; }};
}
const __els = {};
const stubs = `
var O = null, view = {kind: "command"}, repoTab = "overview", schedCache = {},
  eventsCache = [], checks = {}, repoTabCache = {}, depFilterQ = "",
  evShown = 6, evShownSig = "", evFocusRow = null;
function $(id) { if (!__els[id]) __els[id] = makeEl(id); return __els[id]; }
var document = {querySelectorAll: function () { return []; },
  activeElement: null, hidden: false, scrollingElement: null,
  addEventListener: function () {}};
var CSS = {escape: function (s) { return String(s); }};
function jget(u) {
  if (String(u).startsWith("/api/events")) return Promise.resolve({events: []});
  return Promise.reject(new Error("unexpected GET " + u));
}
function jpost() { return Promise.reject(new Error("no POST in render test")); }
function toast() {}
function focusViewHeading() { return false; }
function render() {}
function renderEmpty() {}
function openWork() {}
function openRepo() {}
function syncHash() {}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
`;
const tests = `
;(async () => {
// one repo with derived review counts and raw queue history: the exact
// shape that threw ReferenceError: statline is not defined in Chrome.
O = {repos: [{name: "react-native-nitro-sound", github: "hyochan/react-native-nitro-sound",
    has_next: true, drifted: 43, sched_enabled: true, execute_enabled: false,
    last_scan: "2026-10-18T09:50:32+00:00", next_scan: "2026-10-18T10:20:32+00:00",
    queue_counts: {"queued": 31, "running": 0, "blocked": 0, "waiting-review": 14}}],
  runs: [{id: "run-q1", repo: "react-native-nitro-sound", issue: "279",
    title: "Raw Buffer support", created_at: "2026-10-18T09:00:00+00:00",
    waitReason: "question: Which platforms need raw buffers?",
    steps: {triage: {status: "done"}}},
    {id: "run-done1", repo: "react-native-nitro-sound", issue: "774",
    title: "jest 30.5.2", created_at: "2026-10-18T08:00:00+00:00",
    waitReason: "", steps: {triage: {status: "done"}, review: {status: "done"}}}],
  jobs: [], chats: [],
  review_batches: {"react-native-nitro-sound": {reviewed: 14, validating: 0,
    failed: 14, ready: 0, unique: 14, next_action: "막힘 14건 원인 해소"}},
  maintenance_service: {}, worker_lock: null};
// sched cache prefilled so no sched fetch happens during render
schedCache["react-native-nitro-sound"] = {queue: [], last_rows: []};
await renderCommand();
const out = __els["view"].innerHTML;
ok(out.includes("관제센터"), "command heading renders");
ok(out.includes("검토 묶음"), "derived review projection is primary");
ok(out.includes("1차 검토 완료 14"), "derived counts show accepted 14");
ok(out.includes("자동 처리 기록 (참고용)"), "raw queue history under secondary disclosure");
ok(out.includes("리뷰 대기") && out.includes("14"), "raw waiting-review count kept as history");
ok(out.includes("Which platforms need raw buffers?"), "question text reaches the decision card");
ok(out.includes("검토 작업실"), "primary action routes to the review workspace");
console.log("PASS " + pass);
})();
`;
function extractConstLine(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=[^\\n;]+;");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const line " + name);
  return mm[0].startsWith("\n") ? mm[0].slice(1) : mm[0];
}
const pieces = [stubs];
for (const n of wantBlock) pieces.push(extractConstBlock(n));
for (const n of wantLine) pieces.push(extractFnLine(n));
for (const n of wantConst) pieces.push(extractConstLine(n));
for (const n of wantFn) pieces.push(extractFn(n));
pieces.push(tests);
eval(pieces.join("\n"));
'''


def _run_node():
    return subprocess.run(
        ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS),
         ",".join(BLOCK_CONSTS), ",".join(LINE_FUNCTIONS),
         ",".join(CONST_LINES)],
        capture_output=True,
        text=True,
        timeout=60,
    )


class CommandRenderExecTest(unittest.TestCase):
    def test_render_command_executes_with_derived_and_raw(self):
        proc = _run_node()
        self.assertEqual(
            proc.returncode, 0, f"node driver failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertNotIn("FAIL", proc.stdout, f"logic failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")
