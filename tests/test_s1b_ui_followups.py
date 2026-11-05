"""S1b regressions: 390px header wrap, src.status Korean routing,
broadest-ready prepare default (dashboard.html).

Strategy mirrors tests/test_s1_ui_backlog.py: pure helpers are extracted
from the shipped ``src/openmainty/dashboard.html`` inline script and run
under node. Inline render sites that cannot be extracted as pure functions
are guarded by structural asserts (exact identifiers/wording present);
their visible behavior is verified in the real browser during self-use,
stated explicitly per test.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

DRIVER_HEAD = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
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
function extractConstBlock(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=\\s*\\{");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const block " + name);
  const brace = src.indexOf("{", mm.index);
  return "const " + name + " = " + src.slice(brace, sliceBalanced(src, brace)) + ";";
}
const wantFn = (process.argv[3] || "").split(",").filter(Boolean);
const wantBlock = (process.argv[4] || "").split(",").filter(Boolean);
let seized = "";
for (const f of wantFn) seized += extractFn(f) + "\n";
for (const b of wantBlock) seized += extractConstBlock(b) + "\n";
const driverBody = process.argv[5];
const prog = seized + "\n" + driverBody;
try {
  eval(prog);
} catch (e) {
  console.log("FAIL driver eval: " + (e && e.message));
  process.exit(1);
}
'''

BODY_STATUS = """
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
eq(srcStatusKo("devtools-verified"), "개발 도구 검증됨", "known status mapped");
eq(srcStatusKo("done"), "완료", "gst known status mapped");
eq(srcStatusKo("mystery-state"), "상태: mystery-state", "unknown status korean fallback");
eq(srcStatusKo(""), "", "empty status stays empty");
eq(srcStatusKo(null), "", "null status stays empty");
console.log("STATUS-PASS " + pass);
"""

BODY_BROADEST = """
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function nx(id, ready, primaries, at, inherited) {
  return {id: id, ready: ready, primaries: primaries, updated_at: at,
    inherited: inherited, integrations: [{lane: "next"}]};
}
/* 3-batch fixture: base + two leaves inheriting it. Siblings must not
   include each other, so each leaf's closure holds exactly the base;
   the confirmed tie resolves to the common ancestor, never a leaf. */
const fam = [
  nx("base", 2, 2, "2026-10-19T01:00:00+00:00", []),
  nx("leaf-a", 1, 1, "2026-10-19T02:00:00+00:00", [{batch: "base"}]),
  nx("leaf-b", 1, 1, "2026-10-19T03:00:00+00:00", [{batch: "base"}])
];
const byId = {};
fam.forEach(b => { byId[b.id] = b; });
eq(batchClosureReady(byId["base"], byId), 0, "base closure holds no other batch");
eq(batchClosureReady(byId["leaf-a"], byId), 1, "leaf closure holds only the base");
eq(batchClosureReady(byId["leaf-b"], byId), 1, "sibling leaves exclude each other");
const fb = broadestReady(fam, "next", false);
eq(fb.id, "base", "sibling tie resolves to the common ancestor");
eq(fb.viaAncestor, true, "ancestor path flagged");
eq(fb.tie, 2, "tie counts the sibling leaves");
eq(defaultBatch(fam, "next", ""), "base", "default prefers the common ancestor");
/* A non-ready batch with a wider closure must never become the default. */
const withStale = fam.concat([
  nx("stale-big", 0, 1, "2026-10-19T04:00:00+00:00",
    [{batch: "base"}, {batch: "leaf-a"}, {batch: "leaf-b"}])
]);
eq(defaultBatch(withStale, "next", ""), "base", "non-ready batch never defaulted");
/* (a) linear chain: the unique deepest batch wins, no ancestor path. */
const chain = [
  nx("c0", 1, 1, "2026-10-19T01:00:00+00:00", []),
  nx("c1", 1, 1, "2026-10-19T02:00:00+00:00", [{batch: "c0"}]),
  nx("c2", 1, 1, "2026-10-19T03:00:00+00:00", [{batch: "c1"}])
];
eq(defaultBatch(chain, "next", ""), "c2", "chain defaults to deepest");
eq(broadestReady(chain, "next", false).viaAncestor, false, "unique max needs no ancestor");
/* (b) real-shaped: grandparent -> parent -> 3 sibling leaves. The tie
   resolves to the deepest common ancestor, with all leaves counted. */
const clan = [
  nx("g", 1, 1, "2026-10-19T00:00:00+00:00", []),
  nx("a", 2, 2, "2026-10-19T01:00:00+00:00", [{batch: "g"}]),
  nx("l1", 1, 1, "2026-10-19T02:00:00+00:00", [{batch: "a"}]),
  nx("l2", 1, 1, "2026-10-19T03:00:00+00:00", [{batch: "a"}]),
  nx("l3", 1, 1, "2026-10-19T04:00:00+00:00", [{batch: "a"}])
];
const rb = broadestReady(clan, "next", false);
eq(rb.id, "a", "leaf tie resolves to deepest common ancestor");
eq(rb.tie, 3, "tie counts all sibling leaves");
eq(rb.viaAncestor, true, "clan ancestor path flagged");
eq(defaultBatch(clan, "next", ""), "a", "default is the common ancestor");
/* (c) disjoint chains with no common listed ancestor: no auto default. */
const split = [
  nx("p1", 1, 1, "2026-10-19T01:00:00+00:00", []),
  nx("p2", 1, 1, "2026-10-19T01:00:00+00:00", []),
  nx("s1", 1, 1, "2026-10-19T02:00:00+00:00", [{batch: "p1"}]),
  nx("s2", 1, 1, "2026-10-19T03:00:00+00:00", [{batch: "p2"}])
];
const sb = broadestReady(split, "next", false);
eq(sb.id, "", "no common ancestor means no auto default");
eq(sb.tie, 2, "unresolved tie still counted");
eq(defaultBatch(split, "next", ""), "", "default empty without common ancestor");
/* A next candidate with only a main base falls back to the allowed main
   base instead of an empty default; a main candidate with only next
   batches gets no default (lane rule: main on next is refused). */
const mainOnly = [
  {id: "m-base", ready: 1, primaries: 1, updated_at: "2026-10-19T01:00:00+00:00",
   integrations: [{lane: "main"}]}
];
eq(defaultBatch(mainOnly, "next", ""), "m-base", "next falls back to allowed main base");
eq(defaultBatch(mainOnly, "main", ""), "m-base", "main default on main base");
eq(defaultBatch(fam, "main", ""), "", "main candidate never defaults to next");
eq(defaultBatch([], "next", ""), "", "empty eligible");
console.log("BROADEST-PASS " + pass);
"""


def _run_node(fns, blocks, body):
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".js", delete=False,
                                encoding="utf-8") as fh:
        fh.write(DRIVER_HEAD)
        driver = fh.name
    try:
        proc = subprocess.run(
            ["node", driver, str(HTML),
             ",".join(fns), ",".join(blocks), body],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            Path(driver).unlink()
        except OSError:
            pass
    return proc


class S1bHeaderWrapTests(unittest.TestCase):
    """390px header: chips wrap below the title at <=420px (structural;
    visible wrapping verified in the real browser during self-use)."""

    def _src(self):
        return HTML.read_text(encoding="utf-8")

    def test_narrow_media_rule(self):
        src = self._src()
        self.assertIn("@media (max-width: 420px)", src)
        self.assertIn(".bar { flex-wrap: wrap;", src)
        self.assertIn(".bartitle { flex: 1 1 55%; min-width: 55%;", src)

    def test_title_ellipsis_kept(self):
        src = self._src()
        self.assertIn(".bartitle { font-size: 14px; font-weight: 600; "
                      "flex: 1; min-width: 0; white-space: nowrap;", src)
        self.assertIn("text-overflow: ellipsis;", src)


class S1bSrcStatusTests(unittest.TestCase):
    """src.status renders Korean: GST_KO hit or '상태: <raw>' fallback."""

    def test_status_ko(self):
        proc = _run_node(["srcStatusKo"], ["GST_KO"], BODY_STATUS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS-PASS 5", proc.stdout)

    def test_call_site_routed(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("esc(srcStatusKo(src.status))", src)
        self.assertNotIn("esc(src.status)", src)


BODY_RETRY = """
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
eq(retryResultKo("done", ""), "재시도 완료", "done reports completion");
eq(retryResultKo("blocked", "기준이 같음"), "재시도 막힘: 기준이 같음", "blocked names the reason");
eq(retryResultKo("blocked", ""), "재시도 막힘: ", "blocked without reason stays honest");
eq(retryResultKo("started", ""), "재시도 상태: started", "non-done never claims completion");
eq(retryResultKo("", ""), "재시도 상태: 알 수 없음", "missing status stays honest");
console.log("RETRY-PASS " + pass);
"""


class S1bRetryWordingTests(unittest.TestCase):
    """Retry toast/message reflects the actual result (S5-Bii)."""

    def test_retry_result_ko(self):
        proc = _run_node(["retryResultKo"], [], BODY_RETRY)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RETRY-PASS 5", proc.stdout)

    def test_retry_call_site(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("say(retryResultKo(r.status, r.error));", src)
        self.assertIn("toast(retryResultKo(r.status, r.error));", src)
        self.assertNotIn("재시도 기록됨", src)


class S1bBroadestDefaultTests(unittest.TestCase):
    """Prepare base default prefers the broadest READY same-lane batch."""

    def test_broadest_ready(self):
        proc = _run_node(
            ["prepBatchLanes", "prepBaseFullyReady", "batchClosureReady",
             "broadestReady", "defaultBatch"],
            [], BODY_BROADEST)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BROADEST-PASS 21", proc.stdout)

    def test_reason_line_wiring(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("기본 선택: 가장 넓은 준비 완료 묶음(계승 ${auto.kin}건 포함)", src)
        self.assertIn("기본 선택: 공통 기준", src)
        self.assertIn("같은 깊이의 형제 묶음 ${auto.tie}개는 서로 포함하지 않음", src)
        self.assertIn("기본 선택 없음 — 같은 깊이의 형제 묶음 ${auto.tie}개 중 직접 고르세요", src)
        # Each reason renders only on the automatic default, never on the
        # user's own select change.
        self.assertEqual(src.count("기본 선택: 가장 넓은 준비 완료 묶음"), 1)
        self.assertEqual(src.count("기본 선택: 공통 기준"), 1)
        self.assertEqual(src.count("기본 선택 없음 —"), 1)


BODY_GLANCE = """
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
location = {hash: ""};
eq(parseHash().kind, "glance", "empty hash lands on glance");
location = {hash: "#/"};
eq(parseHash().kind, "glance", "bare hash lands on glance");
location = {hash: "#"};
eq(parseHash().kind, "glance", "lone hash lands on glance");
location = {hash: "#/command"};
eq(parseHash().kind, "command", "explicit command kept");
location = {hash: "#/glance"};
eq(parseHash().kind, "glance", "explicit glance kept");
location = {hash: "#/digest"};
eq(parseHash().kind, "digest", "explicit digest kept");
const rank = [glanceUrgRank("blocked"), glanceUrgRank("unknown"),
  glanceUrgRank("attention"), glanceUrgRank("normal"),
  glanceUrgRank("mystery")];
eq(rank[0] < rank[1] && rank[1] < rank[2] && rank[2] < rank[3], true,
  "cards order blocked > unknown > attention > normal");
eq(rank[3] < rank[4], true, "unknown verdict sorts last");
const rows = [
  {name: "cal", verdict: "normal", need_you: []},
  {name: "nitro", verdict: "attention",
   need_you: [{}, {}, {}, {}, {}, {}, {}]},
  {name: "other", verdict: "attention", need_you: [{}]}
];
const sorted = glanceSortRows(rows).map(r => r.name);
eq(JSON.stringify(sorted), JSON.stringify(["nitro", "other", "cal"]),
  "urgency first, then need-you count (tonight: nitro before cal)");
eq(rows[0].name, "cal", "sort does not mutate the input");
console.log("GLANCE-PASS " + pass);
"""


class AuditGlanceTests(unittest.TestCase):
    """M1.2 audit fixes: the glance is the default page, ordered by
    urgency, with the stored digest title and a data-derived next step.
    Behavior runs under node; boot ordering and render wiring are
    structural (the init IIFE and renderers need the live DOM)."""

    def test_default_route_behavior(self):
        proc = _run_node(["parseHash", "glanceUrgRank", "glanceSortRows"],
                         [], BODY_GLANCE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("GLANCE-PASS 10", proc.stdout)

    def test_rollup_error_persists(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("st.error = \"합치기 실패: \" + e.message", src)
        self.assertIn("${esc(st.error || \"\")}</div>", src)

    def test_summary_first_boot(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn('O = O || {repos: [], runs: [], _shell: true};', src)
        boot = src[src.index("} else view = {kind: \"glance\"};"):]
        boot = boot[:boot.index("})();")]
        self.assertLess(boot.index("render();"), boot.index("loadAll(true)"),
                        "glance paints from /api/summary before overview")

    def test_digest_renders_stored_title(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("data.title ||", src)
        self.assertNotIn("<h1>하루 요약 (${esc(data.date", src)

    def test_next_step_and_branch_days(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("r.next_steps", src)
        self.assertIn("검토 작업실에서 보기", src)
        self.assertIn("일 전)", src)
        self.assertIn('details class="storymore"', src)

    def test_narrow_toast_and_story_rules(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn(".storymore:not([open]) > :not(summary)", src)
        self.assertIn(".toast { top: 0; bottom: auto;", src)


BODY_REVIEWS_EARLY = r"""
/* S7.2 behavior rig: O is null (overview still in flight) and overview
   NEVER resolves. The reviews panel + cards must still paint from the
   fast /api/batches envelope, fetched exactly once across paints. */
let O = null;
let view = {kind: "repo", id: "nitro"};
let repoTab = "reviews";
let batchesInflight = null, batchesInflightAt = 0;
let revReqSeq = 0;
let batchesCache = null, batchesFp = "", batchesObs = "";
let batchesSemCards = {}, batchesSemSummary = "";
const batchOpen = new Set();
const rollupUi = {};
const CSS = {escape: (s) => String(s)};
/* esc can't be machine-extracted (a quote inside its regex literal
   defeats the driver's brace matcher), so reimplement it 1:1 here. */
function esc(s) {
  const m = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"};
  return String(s ?? "").replace(/[&<>"]/g, (c) => m[c]);
}
function mkEl() {
  return {_html: "", dataset: {}, textContent: "",
    classList: {add() {}, remove() {}},
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    querySelector(s) { return null; },
    querySelectorAll(s) { return []; }};
}
const viewEl = mkEl(), tabEl = mkEl();
function $(id) { return id === "view" ? viewEl : tabEl; }
function toast(m) {}
function takeStashedReviewState() { return null; }
function captureReviewState(box) { return null; }
function revStaleGuard() { return false; }
async function restoreReviewStateAsync(box, keep) {}
function wireRollupPanel(box, r, mine) {}
function openBatchImport(name) {}
function renderRepo() {}
let batchesCalls = 0, overviewCalls = 0;
const ENVELOPE = {
  batches: [
    {id: "base", repo: "nitro", target_branch: "next", purpose: "base",
     primaries: 2, updated_at: "2026-10-19T01:00:00+00:00", inherited: [],
     integrations: [{lane: "next", worktree: "w", status: "done",
       head: "a", fingerprint: "b", live_head: "a", live_fingerprint: "b",
       live_clean: true,
       gates: [{key: "unit", status: "pass",
         inputs: {head: "a", fingerprint: "b"}, evidence: []}]}],
     ready: 2, obs: "obs-test",
     evaluation: {ready: 2, total: 2, first_failure: null}},
    {id: "leaf-a", repo: "nitro", target_branch: "next", purpose: "a",
     primaries: 1, updated_at: "2026-10-19T02:00:00+00:00",
     inherited: [{group: "next", batch: "base", count: 1}],
     integrations: [{lane: "next", worktree: "w", status: "done",
       head: "a", fingerprint: "b", live_head: "a", live_fingerprint: "b",
       live_clean: true,
       gates: [{key: "unit", status: "pass",
         inputs: {head: "a", fingerprint: "b"}, evidence: []}]}],
     ready: 1, obs: "obs-test",
     evaluation: {ready: 1, total: 1, first_failure: null}},
    {id: "leaf-b", repo: "nitro", target_branch: "next", purpose: "b",
     primaries: 1, updated_at: "2026-10-19T03:00:00+00:00",
     inherited: [{group: "next", batch: "base", count: 1}],
     integrations: [{lane: "next", worktree: "w", status: "done",
       head: "a", fingerprint: "b", live_head: "a", live_fingerprint: "b",
       live_clean: true,
       gates: [{key: "unit", status: "pass",
         inputs: {head: "a", fingerprint: "b"}, evidence: []}]}],
     ready: 1, obs: "obs-test",
     evaluation: {ready: 1, total: 1, first_failure: null}}
  ],
  summary: {nitro: {reviewed: 0, validating: 0, failed: 0, ready: 2,
    next_action: "x"}},
  unregistered: {}, obs: "obs-test", at: "2026-10-20T00:00:00+00:00"
};
function jget(url) {
  if (url === "/api/batches") {
    batchesCalls++;
    return new Promise((res) =>
      setTimeout(() => res(JSON.parse(JSON.stringify(ENVELOPE))), 10));
  }
  overviewCalls++;
  return new Promise(() => {});
}
(async () => {
  await paintRepoReviews({name: "nitro"});
  await paintRepoReviews({name: "nitro"});
  const html = tabEl.innerHTML;
  console.log(html.includes("레인 합치기") ? "PANEL-YES" : "PANEL-NO");
  console.log(html.includes("leaf-a") ? "CARDS-YES" : "CARDS-NO");
  console.log(batchesCalls === 1 ? "FETCH-1" : "FETCH-" + batchesCalls);
  console.log(overviewCalls === 0 ? "OVERVIEW-0"
                                  : "OVERVIEW-" + overviewCalls);
  console.log("DONE");
  process.exit(0);
})();
setTimeout(() => { console.log("FAIL timeout"); process.exit(1); }, 20000);
"""


BODY_RUNS_PARTITION = r"""
/* S8-b behavior rig: O.runs holds SLIM summaries (the new overview
   projection — no commands, evidence, artifacts). The reviews
   in-progress partition must still split running vs idle, and the
   runs table must render from summaries without touching overview. */
let O = {
  repos: [{name: "nitro"}],
  runs: [
    {id: "run-live", repo: "nitro", issue: "7", title: "t",
     dep: "a-dep", target: "1.0", lane: "next", branch: "b",
     created_at: "2026-10-20T00:00:00+00:00", waitReason: null,
     steps: {plan: {status: "running",
                    started_at: "2026-10-20T00:00:00+00:00"}},
     disposition: {kind: "ACTIONABLE"}},
    {id: "run-idle", repo: "nitro", issue: "8", title: "t",
     dep: null, target: null, lane: null, branch: null,
     created_at: "2026-10-20T00:00:00+00:00", waitReason: null,
     steps: {plan: {status: "done", started_at: null}},
     disposition: null}
  ]
};
let view = {kind: "repo", id: "nitro"};
let repoTab = "reviews";
let batchesInflight = null, batchesInflightAt = 0;
let revReqSeq = 0;
let batchesCache = null, batchesFp = "", batchesObs = "";
let batchesSemCards = {}, batchesSemSummary = "";
const batchOpen = new Set();
const rollupUi = {};
const CSS = {escape: (s) => String(s)};
function mkEl() {
  return {_html: "", dataset: {}, textContent: "",
    classList: {add() {}, remove() {}},
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    querySelector(s) { return null; },
    querySelectorAll(s) { return []; }};
}
const viewEl = mkEl(), tabEl = mkEl();
function $(id) { return id === "view" ? viewEl : tabEl; }
function toast(m) {}
function takeStashedReviewState() { return null; }
function captureReviewState(box) { return null; }
function revStaleGuard() { return false; }
async function restoreReviewStateAsync(box, keep) {}
function wireRollupPanel(box, r, mine) {}
function openBatchImport(name) {}
function renderRepo() {}
function esc(s) {
  const m = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"};
  return String(s ?? "").replace(/[&<>"]/g, (c) => m[c]);
}
let batchesCalls = 0, otherCalls = [];
const ENVELOPE = {
  batches: [], summary: {},
  unregistered: {nitro: [
    {dep: "a-dep", target: "1.0", lane: "next", run_id: "run-live"},
    {dep: "b-dep", target: "2.0", lane: "next", run_id: "run-idle"}
  ]},
  obs: "obs-test", at: "2026-10-20T00:00:00+00:00"
};
function jget(url) {
  if (url === "/api/batches") {
    batchesCalls++;
    return new Promise((res) =>
      setTimeout(() => res(JSON.parse(JSON.stringify(ENVELOPE))), 10));
  }
  otherCalls.push(url);
  return new Promise(() => {});
}
function latestPerIssue(runs) {
  const best = {};
  (runs || []).forEach((r) => {
    const k = `${r.repo}#${r.issue}`;
    if (!best[k] || (r.created_at || "") > (best[k].created_at || ""))
      best[k] = r;
  });
  return best;
}
(async () => {
  await paintRepoReviews({name: "nitro"});
  const html = tabEl.innerHTML;
  const liveIdx = html.indexOf("진행 중"), idleIdx = html.indexOf("b-dep");
  console.log(html.includes("a-dep") && liveIdx !== -1
    ? "LIVE-YES" : "LIVE-NO");
  console.log(html.includes("미등록 검토 완료") && idleIdx !== -1
    ? "IDLE-YES" : "IDLE-NO");
  console.log(otherCalls.length === 0 ? "NO-OVERVIEW"
                                      : "EXTRA-" + otherCalls.join(","));
  const rows = Object.values(latestPerIssue(O.runs));
  console.log(rows.length === 2 ? "TABLE-2" : "TABLE-" + rows.length);
  console.log("DONE");
  process.exit(0);
})();
setTimeout(() => { console.log("FAIL timeout"); process.exit(1); }, 20000);
"""


BODY_DEEPLINK = """
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
location = {hash: "#/repo/nitro?tab=reviews&batch=auto-x"};
let v = parseHash();
eq(v.kind, "repo", "deep link is a repo view");
eq(v.tab, "reviews", "deep link keeps the reviews tab");
eq(v.batch, "auto-x", "deep link carries the batch id");
location = {hash: "#/repo/nitro?tab=reviews"};
eq(parseHash().batch, "", "plain reviews tab has no batch");
location = {hash: "#/glance"};
eq(parseHash().batch, undefined, "non-repo views carry no batch key");
let pendingBatchScroll = "b1";
const batchOpen = new Set();
const CSS = {escape: (s) => String(s)};
// New whole-row/single-inspector DOM: the row is one button carrying
// data-batchdetail; closest("[data-batch]") finds its card for scroll.
let clicked = false, scrolledTo = false;
const card = { scrollIntoView: (o) => { scrolledTo = true; } };
const btn = { dataset: {batchdetail: "b1"},
  click: () => { clicked = true; },
  closest: (s) => s === "[data-batch]" ? card : null };
const box = {querySelector: (s) => s === '[data-batchdetail="b1"]' ? btn : null};
// Pre-add path inside paintRepoReviews replaces any previous selection.
batchOpen.add("old-sel");
selectOnly(batchOpen, pendingBatchScroll);
eq(batchOpen.has("b1"), true, "deep link selects the batch");
eq(batchOpen.has("old-sel"), false, "previous selection replaced");
eq(batchOpen.size, 1, "exactly one inspector");
scrollPendingBatch(box);
eq(clicked, false, "already-selected row is not re-clicked");
eq(pendingBatchScroll, "", "deep link is one-shot");
pendingBatchScroll = "b1";
batchOpen.clear();
scrollPendingBatch(box);
eq(clicked, true, "fresh deep link expands the row");
eq(scrolledTo, true, "card is scrolled into view");
eq(pendingBatchScroll, "", "deep link stays one-shot");
console.log("DEEPLINK-PASS " + pass);
"""


BODY_ROLLUP_STORED = r"""
/* M2.25b: the reviews tab shows the latest stored rollup draft from
   the overview-shaped repo row (as /api/overview now returns it),
   with zero in-session result. PR bodies, PR1/PR2, caveats and the
   redraft button render; the ?rollup= lane link scrolls after the
   card exists. */
let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function has(s, sub) { return s.indexOf(sub) >= 0; }
function esc(s) {
  const m = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"};
  return String(s ?? "").replace(/[&<>"]/g, (c) => m[c]);
}
const CSS = {escape: (s) => String(s)};
let O = {repos: [], runs: []};
let view = {kind: "repo", id: "nitro"};
let repoTab = "reviews";
let batchesInflight = null, batchesInflightAt = 0;
let revReqSeq = 0;
let batchesCache = null, batchesFp = "", batchesObs = "";
let batchesSemCards = {}, batchesSemSummary = "";
const batchOpen = new Set();
const rollupUi = {};
const scrolled = {lanes: []};
function mkCard(lane) {
  return {lane, querySelector(s) { return null; },
    querySelectorAll(s) { return []; },
    scrollIntoView() { scrolled.lanes.push(lane); }};
}
function mkEl() {
  return {_html: "", dataset: {}, textContent: "",
    classList: {add() {}, remove() {}},
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    querySelector(s) {
      const m = /\[data-rollup="([^"]+)"\]/.exec(s);
      if (m && this._html.indexOf('data-rollup="' + m[1] + '"') >= 0) return mkCard(m[1]);
      return null;
    },
    querySelectorAll(s) { return []; }};
}
const viewEl = mkEl(), tabEl = mkEl();
function $(id) { return id === "view" ? viewEl : tabEl; }
function toast(m) {}
function takeStashedReviewState() { return null; }
function captureReviewState(box) { return null; }
function revStaleGuard() { return false; }
async function restoreReviewStateAsync(box, keep) {}
function wireRollupPanel(box, r, mine) {}
function openBatchImport(name) {}
function renderRepo() {}
const ROW_NEW = {op: "op-9", status: "done", leaves: 9,
  leaves_list: [{batch: "b1", dep: "jest", target: "1.0.0"}],
  branch: "br", head: "abcdef1234567890",
  result: {branch: "br", head: "abcdef1234567890"},
  pr_title: "STORED-TITLE<script>", pr_body: "hello<script>",
  stacked: {summary_ko: "sum", pr1: {title: "p1", body: "b1"}, pr2: {title: "p2", body: "b2"}},
  owner_commands: ["git push"], gates: [{key: "fastchecks", status: "pending"}],
  caveats: [{leaf: "b1", kind: "compiler", text: "c1"}, {leaf: "b1", kind: "newer_ready", text: "c2"}]};
const ROW_OLD = {op: "op-old", status: "done", leaves: 9,
  branch: "br", head: "abcdef1234567890",
  pr_title: "OLD-TITLE", pr_body: "old-body",
  stacked: {summary_ko: "sum", pr1: {title: "p1", body: "b1"}},
  caveats: [{leaf: "b1", kind: "compiler", text: "c1"}]};
const SESSION = {op_id: "op-new", status: "done", leaves: [],
  result: {}, pr_draft: {title: "SESS-TITLE", body: "sess"},
  owner_commands: [], gates: [], caveats: []};
const ENVELOPE = {
  batches: [], summary: {nitro: {reviewed: 0, validating: 0, failed: 0, ready: 0, next_action: "x"}},
  unregistered: {}, obs: "obs-test", at: "2026-10-25T00:00:00+00:00"};
function jget(url) {
  if (url === "/api/batches") {
    return new Promise((res) =>
      setTimeout(() => res(JSON.parse(JSON.stringify(ENVELOPE))), 10));
  }
  return new Promise(() => {});
}
let pendingRollupScroll = "next";
(async () => {
  await paintRepoReviews({name: "nitro", rollup: {next: ROW_NEW}});
  const html = tabEl.innerHTML;
  eq(has(html, "STORED-TITLE"), true, "stored title renders");
  eq(has(html, "&lt;script&gt;"), true, "body escaped");
  eq(has(html, "<script>"), false, "no raw script");
  eq(has(html, "초안 이후 확인할 점 2건"), true, "caveat count");
  eq(has(html, 'data-k="rollup-body"'), true, "body textarea");
  eq((html.match(/data-k="rollup-stack-body"/g) || []).length, 2, "pr1+pr2 textareas");
  eq(has(html, "PR1 — main 기준 체인"), true, "pr1 label");
  eq(has(html, "PR2 — next 합치기"), true, "pr2 label");
  eq(has(html, 'data-op="op-9"'), true, "redraft button op");
  eq(has(html, "합칠 준비된 묶음 없음"), true, "stored-only card note");
  eq(scrolled.lanes.join(","), "next", "scrolls to lane card after render");
  eq(pendingRollupScroll, "", "rollup scroll is one-shot");
  rollupUi["nitro|next"] = {selected: {}, result: SESSION};
  const html2 = rollupPanelHtml({name: "nitro", rollup: {next: ROW_NEW}}, [], "next");
  eq(has(html2, "SESS-TITLE"), true, "session wins");
  eq(has(html2, "STORED-TITLE"), false, "stored hidden when session");
  const recOld = storedRollupRec(ROW_OLD);
  eq(recOld && recOld.op_id, "op-old", "old row maps");
  const htmlOld = rollupResultHtml(recOld);
  eq(has(htmlOld, "OLD-TITLE"), true, "old row renders");
  eq(has(htmlOld, 'data-op="op-old"'), true, "old redraft button");
  eq(has(htmlOld, "9개 묶음 합침"), true, "old row keeps leaf count");
  eq(has(htmlOld, "0개 묶음 합침"), false, "no zero-count chip");
  location = {hash: "#/repo/nitro?tab=reviews&rollup=next"};
  eq(parseHash().rollup, "next", "hash carries lane");
  location = {hash: "#/repo/nitro?tab=reviews"};
  eq(parseHash().rollup, "", "plain reviews has no lane");
  console.log("STORED-LOADER-PASS " + pass);
  process.exit(0);
})();
setTimeout(() => { console.log("FAIL timeout"); process.exit(1); }, 20000);
"""


class AuditReviewsTests(unittest.TestCase):
    """S7.1-a/b: the reviews tab paints from /api/batches without
    waiting for overview, and the stamp reads '목록 기준 N분 전'."""

    def test_stored_rollup_renders_from_overview_row(self):
        # M2.25b: paintRepoReviews shows the stored done lane from
        # an overview-shaped repo row (as /api/overview now
        # returns it) with zero in-session result; the ?rollup=
        # link scrolls after the card exists; an in-session result
        # still wins; old-shape rows render without error.
        proc = _run_node(
            ["paintRepoReviews", "fetchBatchesOnce", "rollupPanelHtml",
             "rollupCandidates", "broadestReady", "batchClosureReady",
             "prepBatchLanes", "rollupKey", "sharedBlockerLine",
             "batchCardTitle", "pendingChip", "selectOnly", "toggleSingle",
             "reviewsContentFp", "revCardSem", "revBound160", "revSrcSig",
             "batchesAgeKo", "batchPhaseKo", "batchNextAction",
             "fmtKSThm", "fmtKST", "gateStale", "prepBaseFullyReady",
             "rollupResultHtml", "storedRollupRec",
             "effectiveRollupRec", "stackedPrHtml", "scrollPendingRollup",
             "parseHash"],
            ["INTEG_KO"],
            BODY_ROLLUP_STORED)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STORED-LOADER-PASS 21", proc.stdout)

    def test_deeplink_batch_param(self):
        # M2.5b: the glance hold/waiting button opens that batch's
        # card via ?tab=reviews&batch=<id>.
        proc = _run_node(["parseHash", "scrollPendingBatch", "selectOnly"],
                         [], BODY_DEEPLINK)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DEEPLINK-PASS 13", proc.stdout)

    def test_reviews_first_boot(self):
        src = HTML.read_text(encoding="utf-8")
        init = src[src.index("(function init() {"):]
        init = init[:init.index("})();")]
        branch = init[init.index('repoTab === "reviews"'):]
        self.assertLess(
            branch.index("paintReviewsEarly("),
            branch.index("loadAll("),
            "reviews paints from /api/batches before overview")

    def test_early_shell_needs_name_only(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("function paintReviewsEarly(name)", src)
        self.assertIn("paintRepoReviews({name})", src)

    def test_stamp_no_double_baseline(self):
        src = HTML.read_text(encoding="utf-8")
        self.assertIn("목록 기준 ${esc(batchesAgeKo(data.at))}", src)
        self.assertNotIn("분 전 기준", src)

    def test_runs_partition_from_summaries(self):
        # S8-b behavior: slim O.runs (no commands/evidence/artifacts)
        # still split in-progress vs idle, with no overview fetch.
        proc = _run_node(
            ["paintRepoReviews", "fetchBatchesOnce", "rollupPanelHtml",
             "rollupCandidates", "broadestReady", "batchClosureReady",
             "prepBatchLanes", "rollupKey", "sharedBlockerLine",
             "batchCardTitle", "pendingChip", "selectOnly", "toggleSingle",
             "reviewsContentFp", "revCardSem", "revBound160", "revSrcSig",
             "batchesAgeKo", "batchPhaseKo", "batchNextAction",
             "fmtKSThm", "fmtKST", "gateStale", "prepBaseFullyReady",
             "rollupResultHtml", "storedRollupRec",
             "effectiveRollupRec", "stackedPrHtml"],
            ["INTEG_KO"],
            BODY_RUNS_PARTITION)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("LIVE-YES", proc.stdout)
        self.assertIn("IDLE-YES", proc.stdout)
        self.assertIn("NO-OVERVIEW", proc.stdout)
        self.assertIn("TABLE-2", proc.stdout)
        self.assertIn("DONE", proc.stdout)

    def test_runs_consumers_need_no_dropped_fields(self):
        # S8-b structural: every O.runs field the dashboard reads is in
        # the summary projection; detail views fetch full records.
        src = HTML.read_text(encoding="utf-8")
        self.assertIn('jget("/api/run?id="', src)
        self.assertIn('jget("/api/work?run="', src)
        start = src.index("function renderRuns()")
        end = src.index("\nasync function detail(")
        table = src[start:end]
        for deep in (".artifacts", ".command", ".evidence",
                     "workTreeFingerprint", "interventions",
                     ".session_id", ".usage"):
            self.assertNotIn(deep, table)

    def test_panel_paints_before_overview_single_fetch(self):
        # S7.2 behavior: O is null and overview never resolves, yet the
        # rollup panel + cards paint from one /api/batches envelope.
        proc = _run_node(
            ["paintRepoReviews", "fetchBatchesOnce", "rollupPanelHtml",
             "rollupCandidates", "broadestReady", "batchClosureReady",
             "prepBatchLanes", "rollupKey", "sharedBlockerLine",
             "batchCardTitle", "pendingChip", "selectOnly", "toggleSingle",
             "reviewsContentFp", "revCardSem", "revBound160", "revSrcSig",
             "batchesAgeKo", "batchPhaseKo", "batchNextAction",
             "fmtKSThm", "fmtKST", "gateStale", "prepBaseFullyReady",
             "rollupResultHtml", "storedRollupRec",
             "effectiveRollupRec", "stackedPrHtml"],
            ["INTEG_KO"],
            BODY_REVIEWS_EARLY)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PANEL-YES", proc.stdout)
        self.assertIn("CARDS-YES", proc.stdout)
        self.assertIn("FETCH-1", proc.stdout)
        self.assertIn("OVERVIEW-0", proc.stdout)
        self.assertIn("DONE", proc.stdout)


if __name__ == "__main__":
    unittest.main()
