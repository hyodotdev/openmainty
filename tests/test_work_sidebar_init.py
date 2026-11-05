"""Focused regression for deep-link work navigation init (dashboard.html).

Direct load of ``/#/work/<run>`` rendered the work body but left the left
sidebar (``#repolist``/``#chatlist``) and nav badges empty, because the
``view.kind === "work"`` branch of the shipped ``loadAll`` returned through
``renderWorkView({quiet: true})`` before ``render()`` (the only caller of
``renderSide()``) ever ran on initial bootstrap.

The test extracts the actual product ``loadAll`` from the shipped inline
script and drives its real control flow with a minimal DOM/``jget`` spy
harness (same pattern as the other UI-logic tests), asserting:

* the initial non-quiet work load initializes the sidebar from the freshly
  fetched overview (both repos) before the quiet work render, without a
  full ``render()``;
* busy/typing and quiet polls keep the prior preserve-body behavior
  (no full render);
* non-work loads still route to the normal ``render()`` (which
  structurally calls ``renderSide()``);
* an overview network failure never initializes navigation from nothing.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];

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
function extractConstLine(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=([^;\\n]*;?)");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const " + name);
  return mm[0].trim();
}

const stubs = `
let O = null, selRepo = "", view = {kind: "work", id: "run-71c1de11"};
let checks = {}, diagFp = {}, graphCache = null, graphRepo = "";
let ovEpoch = null, ovWantFresh = false;
let repoTab = "overview";
const document = {activeElement: null, querySelectorAll: () => [], querySelector: () => null};
const CSS = {escape: (s) => String(s)};
function stashReviewState() {}
function captureRepoTabFocus() { return null; }
function restoreRepoTabFocus() {}
function captureFk() { return null; }
function restoreFk() { return false; }
function refreshChatQA() {}
async function paintRepoReviews() {}
async function paintRepoCommunity() {}
async function paintRepoPrs() {}
async function paintRepoDeps() {}
async function paintRepoActivity() {}
const seenQ = new Set();
let busyFlag = false, typingFlag = false;
let order = [], sideRepos = -1;
let lastRenderResetScroll = false, lastRouteKey = null;
let jgetCalls = [], jgetMode = "ok", jgetData = null;
const OVERVIEW = {repos: [{name: "react-native-nitro-sound"}, {name: "flutter_calendar_carousel"}],
  runs: [], jobs: [], chats: [{id: "c1"}]};
const dom = {"netdot-dot": {}, "netdot-tx": {}, view: {innerHTML: "", scrollTop: 0}, input: null};
function $(id) { return dom[id]; }
function esc(s) { return String(s == null ? "" : s); }
const toasts = [];
function toast(m) { toasts.push(m); }
function composerBusy() { return busyFlag; }
function workTyping() { return typingFlag; }
function paintStatusbar() { order.push("status"); }
function graphPollRefresh() { order.push("graph"); }
function diagRepo(id) { order.push("diag:" + id); }
function runsFp() { return ""; }
function renderSide() { order.push("side"); sideRepos = (O && O.repos) ? O.repos.length : -1; }
function renderCounts() { order.push("counts"); }
async function renderWorkView(opts) { order.push("work:" + (opts && opts.quiet ? "quiet" : "full")); }
function render() { order.push("render"); }
async function jget(u) {
  jgetCalls.push(u);
  if (jgetMode === "throw") throw new Error("offline");
  return jgetData;
}
function reset() {
  O = null; selRepo = ""; order = []; sideRepos = -1;
  busyFlag = false; typingFlag = false;
  jgetCalls = []; jgetMode = "ok"; jgetData = OVERVIEW;
  dom.view.innerHTML = "";
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
`;

const tests = `
;(async () => {
// 1) initial deep-link work load: sidebar initialized from the fresh
//    overview (both repos) before the quiet work render; no full render.
reset();
view = {kind: "work", id: "run-71c1de11"};
await loadAll();
eq(jgetCalls[0], "/api/overview", "overview fetched first");
eq(sideRepos, 2, "sidebar initialized with both repos from fresh O");
eq(order.indexOf("side") !== -1 && order.indexOf("work:quiet") !== -1 &&
  order.indexOf("side") < order.indexOf("work:quiet"), true,
  "renderSide runs before the quiet work render");
eq(order.indexOf("render") === -1, true, "initial work load never full-renders");
eq(selRepo, "react-native-nitro-sound", "fresh O selects the first repo");

// 2) busy/typing polls preserve the work body: no sidebar, work, or full render.
reset();
view = {kind: "work", id: "run-71c1de11"};
O = OVERVIEW; busyFlag = true;
await loadAll(true);
eq(JSON.stringify(order), JSON.stringify(["status"]),
  "busy poll updates the statusbar only, preserving body/focus");
reset();
view = {kind: "work", id: "run-71c1de11"};
O = OVERVIEW; typingFlag = true;
await loadAll(true);
eq(JSON.stringify(order), JSON.stringify(["status"]),
  "typing poll updates the statusbar only, preserving body/focus");

// 3) quiet work poll keeps prior behavior: quiet work render, no full render,
// plus badge-only refresh (no sidebar rebuild, no focus/input disturbance).
reset();
view = {kind: "work", id: "run-71c1de11"};
await loadAll(true);
ok(order.indexOf("work:quiet") !== -1, "quiet poll still refreshes the work view quietly");
eq(order.indexOf("render") === -1, true, "quiet poll never full-renders");
eq(order.indexOf("side") === -1, true, "quiet poll never rebuilds the sidebar");
ok(order.indexOf("counts") !== -1, "quiet poll still refreshes nav badges");

// 4) non-work loads still take the normal render path (which owns renderSide).
reset();
view = {kind: "command"};
await loadAll();
eq(order.indexOf("render") !== -1, true, "non-work load routes to render()");
ok(renderSrc.indexOf("renderSide()") !== -1, "render() still owns sidebar init");

// 5) overview network failure never initializes navigation from nothing.
reset();
view = {kind: "work", id: "run-71c1de11"};
jgetMode = "throw";
await loadAll();
eq(O, null, "failed fetch leaves O unset");
eq(order.indexOf("side") === -1 && order.indexOf("render") === -1 &&
  order.indexOf("work:quiet") === -1, true,
  "failed load renders no navigation and no work view");
ok(dom.view.innerHTML.indexOf("서버에 연결하지 못했습니다") !== -1, "first-load failure shows the retry state");
console.log("PASS " + pass);
})();
`;

try {
  const loadAllSrc = extractFn("loadAll");
  const driveSrc = extractFn("driveOverviewEpoch");
  const settleSrc = extractFn("settleOverviewEpoch");
  const applySrc = extractFn("applyOverview");
  const errBlockSrc = extractFn("errBlockHtml");
  const fetchErrSrc = extractFn("fetchErrKo");
  const subErrSrc = extractFn("subErrKo");
  const cstart = src.indexOf("const FETCH_ERR_TEXT =");
  if (cstart === -1) throw new Error("missing FETCH_ERR_TEXT");
  const cbrace = src.indexOf("{", cstart);
  const constSrc = src.slice(cstart, sliceBalanced(src, cbrace));
  const subConsts = ["LOGIN_NEEDED_KO", "APIKEY_SET_KO", "SUB_APIKEY_RE", "SUB_LOGIN_RE"]
    .map(extractConstLine).join("\n");
  const renderSrcJson = JSON.stringify(extractFn("render"));
  const p = eval(stubs + constSrc + "\n" + subConsts + "\n" + loadAllSrc + "\n" + driveSrc + "\n" +
    settleSrc + "\n" + applySrc + "\n" + errBlockSrc + "\n" + fetchErrSrc + "\n" + subErrSrc +
    "\nconst renderSrc = " + renderSrcJson + ";\n" + tests);
  Promise.resolve(p).then(() => {}, (e) => { console.log("FAIL driver :: " + (e && e.message)); process.exit(1); });
} catch (e) { console.log("FAIL eval :: " + (e && e.message)); process.exit(1); }
'''


def _run_node():
    return subprocess.run(
        ["node", "-e", DRIVER, str(HTML)],
        capture_output=True,
        text=True,
        timeout=60,
    )


class DashboardScriptTest(unittest.TestCase):
    def test_dashboard_script_syntax(self):
        blocks = re.findall(r"<script>(.*?)</script>", HTML.read_text(), re.S)
        self.assertEqual(len(blocks), 1, "expected one inline dashboard script")
        proc = subprocess.run(
            ["node", "--check", "/dev/stdin"],
            input=blocks[0],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"dashboard JS syntax error: {proc.stderr}")


class WorkSidebarInitTest(unittest.TestCase):
    def test_deeplink_work_load_initializes_sidebar(self):
        proc = _run_node()
        self.assertEqual(
            proc.returncode, 0, f"node driver failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertNotIn("FAIL", proc.stdout, f"logic failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")


if __name__ == "__main__":
    unittest.main()
