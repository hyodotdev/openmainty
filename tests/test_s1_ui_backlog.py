"""S1 regressions: UI backlog wording/selection (dashboard.html) + Korean diffstat.

Covers the S1 slice only: question-card paging (F4), lane-aware prepare
default (F3), inheritance wording dedup, raw-token mapping (F1/F2/vstat),
toast position, sidebar tooltips, 390 header rule, in-progress partition
(F5), quiet-poll badge counts (D4), and the Korean diffstat summary in
``serve._live_source_changes``.

Strategy mirrors tests/test_work_ui_logic.py: pure helpers are extracted
from the shipped ``src/openmainty/dashboard.html`` inline script and run
under node. Inline render sites that cannot be extracted as pure functions
are guarded by structural asserts (exact identifiers/wording present, old
values absent); their visible behavior is verified in the real browser
during self-use, stated explicitly per test.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

sys.path.insert(0, str(ROOT / "src"))

FUNCTIONS = [
    "esc",
    "stepKo",
    "vstatKo",
    "prepBatchLanes",
    "defaultBatch",
    "qaDiagFirst",
    "qaDiagTech",
    "renderCounts",
]
BLOCK_CONSTS = ["STEP_KO", "VSTAT_KO", "GST_KO"]

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
function extractConstLine(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=([^;\\n]*;?)");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const " + name);
  return mm[0].trim();
}
const wantFn = (process.argv[3] || "").split(",").filter(Boolean);
const wantBlock = (process.argv[4] || "").split(",").filter(Boolean);
const wantLine = (process.argv[5] || "").split(",").filter(Boolean);
let seized = "";
for (const f of wantFn) seized += extractFn(f) + "\n";
for (const b of wantBlock) seized += extractConstBlock(b) + "\n";
for (const l of wantLine) seized += extractConstLine(l) + "\n";
const driverBody = process.argv[6];
const prog = seized + "\n" + driverBody;
try {
  eval(prog);
} catch (e) {
  console.log("FAIL driver eval: " + (e && e.message));
  process.exit(1);
}
'''

BODY_PURE = """
/* esc stub: the production esc regex literal cannot be extracted by the
   brace slicer; the real esc is covered by tests/test_work_ui_logic.py.
   All inputs below contain no HTML specials, so passthrough is exact. */
function esc(s) { return String(s); }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
const batches = [
  {id: "m-main", ready: 2, integrations: [{lane: "main"}]},
  {id: "e-next", ready: 2, integrations: [{lane: "next"}]}
];
eq(defaultBatch(batches, "main", ""), "m-main", "main default is first ready main");
eq(defaultBatch(batches, "next", ""), "e-next", "next default skips earlier ready main");
eq(defaultBatch(batches, "next", "m-main"), "e-next", "cross-lane memory ignored");
eq(defaultBatch(batches, "main", "m-main"), "m-main", "same-lane memory kept");
eq(defaultBatch([], "next", ""), "", "empty eligible");
eq(vstatKo("unknown"), "미확인", "vstat unknown mapped");
const line = qaDiagFirst({reason_code: "run_proof_stale",
  vstat: {review: "unknown", e2e: "stale"}});
ok(line.indexOf("검토=미확인") >= 0, "review step mapped");
ok(line.indexOf("검증=바뀐 상태") >= 0, "e2e value mapped");
ok(line.indexOf("review=unknown") < 0, "no raw step token");
const tech = qaDiagTech({reason_code: "rc", category: "cat", op: "op1", rc: 1});
ok(tech.indexOf('title="기술 정보:') > 0, "tech detail in title");
ok(tech.indexOf(">기술 정보: ") < 0, "no inline tech label");
ok(tech.slice(-12) === ">기술 정보</div>", "tech label only");
eq(GST_KO["devtools-verified"], "개발 도구 검증됨", "gst devtools-verified");
console.log("PURE-PASS " + pass);
"""

BODY_COUNTS = """
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function mkEl() {
  return {textContent: "", children: [],
    querySelector: function (s) { return null; },
    appendChild: function (c) { this.children.push(c); },
    remove: function () { this.removed = true; },
    classList: {toggle: function () {}},
    setAttribute: function () {}, removed: false};
}
const nc = mkEl(), nr = mkEl();
const ago = mkEl(); ago.textContent = "37건";
const btn = {dataset: {repo: "nitro"},
  querySelector: function (s) { return s === ".ago" ? ago : null; },
  appendChild: function (c) { btn.added = c; }, added: null};
const rl = {querySelector: function (s) {
  return s.indexOf("nitro") >= 0 ? btn : null; }};
function $(id) {
  if (id === "navcmd-cnt") return nc;
  if (id === "navruns-cnt") return nr;
  if (id === "repolist") return rl;
  return null;
}
const CSS = {escape: function (s) { return s; }};
let O = {runs: [{waitReason: "question:a"}, {waitReason: "question:b"},
  {waitReason: "", steps: {t: {status: "running"}}}],
  repos: [{name: "nitro", queue_counts: {"waiting-review": 39}}]};
renderCounts();
eq(nc.textContent, "2건 답변 필요", "question badge fresh");
eq(nr.textContent, "1건 진행", "live badge fresh");
eq(ago.textContent, "39건", "repo count refreshed in place");
eq(("innerHTML" in rl), false, "no sidebar rebuild");
console.log("COUNTS-PASS " + pass);
"""


def _run_node(fns, blocks, lines, body):
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".js", delete=False,
                                encoding="utf-8") as fh:
        fh.write(DRIVER_HEAD)
        driver = fh.name
    try:
        proc = subprocess.run(
            ["node", driver, str(HTML),
             ",".join(fns), ",".join(blocks), ",".join(lines), body],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            Path(driver).unlink()
        except OSError:
            pass
    return proc


class S1PureUiTests(unittest.TestCase):
    """Extracted pure helpers: lane default, vstat/tech mapping, GST key."""

    def test_pure_helpers(self):
        proc = _run_node(
            ["prepBatchLanes", "prepBaseFullyReady", "batchClosureReady",
             "broadestReady", "defaultBatch",
             "qaDiagFirst", "qaDiagTech"],
            ["STEP_KO", "DIAG_CAT_KO", "GST_KO", "VSTAT_KO"],
            ["stepKo", "vstatKo"], BODY_PURE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PURE-PASS 13", proc.stdout)


class S1RenderCountsTests(unittest.TestCase):
    """renderCounts refreshes badges without rebuilding sidebar DOM."""

    def test_counts_without_rebuild(self):
        proc = _run_node(["renderCounts"], [], [], BODY_COUNTS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COUNTS-PASS 4", proc.stdout)


class S1InlineRenderGuards(unittest.TestCase):
    """Structural guards for inline render sites (visible behavior is
    verified in the real browser during self-use, post-restart)."""

    def _src(self):
        return HTML.read_text(encoding="utf-8")

    def test_question_cards_all_visible(self):
        src = self._src()
        self.assertIn("blockedRuns.slice(0, 8)", src)
        self.assertIn('data-dk="q-rest"', src)
        self.assertNotIn("blockedRuns.slice(0, 4)", src)

    def test_inprogress_partition(self):
        src = self._src()
        self.assertIn("unLive", src)
        self.assertIn("unIdle", src)
        self.assertIn("진행 중", src)
        self.assertIn("워커가 끝나면 아래 검토 완료 목록으로 옮깁니다", src)

    def test_inheritance_dedup(self):
        src = self._src()
        self.assertIn('.replace(/검증$/, "")', src)

    def test_sidebar_tooltips(self):
        src = self._src()
        self.assertIn('<span class="nm" title="${esc(r.name)}">', src)
        self.assertIn('<span class="nm" title="${esc(ch.primary)}">', src)

    def test_toast_shows_message_then_hides(self):
        src = self._src()
        # Semantic contract is structural: live-region role and no
        # pointer interception stay in markup/CSS, not in JS literals.
        self.assertIn('id="toast" class="toast hidden" role="status"', src)
        self.assertIn("pointer-events: none;", src)
        body = (
            "let pass = 0;\n"
            "function eq(a, b, msg) {\n"
            "  if (a === b) { pass++; }\n"
            "  else { console.log(\"FAIL \" + msg + \" :: \" +\n"
            "    JSON.stringify(a) + \" !== \" + JSON.stringify(b));\n"
            "    process.exitCode = 1; }\n"
            "}\n"
            "function ok(c, msg) { eq(!!c, true, msg); }\n"
            "const t = {textContent: \"\", _h: 0, isHidden: true, style: {},\n"
            "  classList: {\n"
            "    remove: function (c) { if (c === \"hidden\") t.isHidden = false; },\n"
            "    add: function (c) { if (c === \"hidden\") t.isHidden = true; }}};\n"
            "function $(id) { if (id === \"toast\") return t; return null; }\n"
            "let timerCb = null;\n"
            "function setTimeout(cb, ms) { timerCb = cb; return 7; }\n"
            "function clearTimeout(h) {}\n"
            "toast(\"hello\");\n"
            "eq(t.textContent, \"hello\", \"toast text set\");\n"
            "eq(t.isHidden, false, \"toast visible after call\");\n"
            "ok(typeof timerCb === \"function\", \"hide scheduled\");\n"
            "timerCb();\n"
            "eq(t.isHidden, true, \"toast hidden after timeout\");\n"
            "console.log(\"TOAST-PASS \" + pass);\n"
        )
        proc = _run_node(["toast", "keepToastClearOfFocus"], [], [], body)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TOAST-PASS 4", proc.stdout)

    def test_header_shrink_rule(self):
        src = self._src()
        self.assertIn(".bartitle { font-size: 14px; font-weight: 600; "
                      "flex: 1; min-width: 0;", src)

    def test_quiet_poll_counts(self):
        src = self._src()
        self.assertIn(
            "if (quiet && !composerBusy() && !workTyping()) renderCounts();",
            src)
        self.assertIn("function renderCounts() {", src)


class S1ServeDiffstatKoTests(unittest.TestCase):
    """Korean diffstat summary from the live worktree reader."""

    def test_live_diffstat_korean(self):
        from openmainty import serve
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            self._git(work, "init", "-q")
            self._git(work, "config", "user.email", "t@t")
            self._git(work, "config", "user.name", "t")
            (work / "a.txt").write_text("one\n")
            (work / "b.txt").write_text("uno\n")
            self._git(work, "add", ".")
            self._git(work, "commit", "-qm", "base")
            base = self._git(work, "rev-parse", "HEAD")
            (work / "a.txt").write_text("one\ntwo\nthree\n")
            (work / "b.txt").write_text("uno\ndos\n")
            out = serve._live_source_changes({}, work, base, base, base)
            total = out["filesTotal"]
            self.assertEqual(total, "2개 파일 변경, +3/-0")
            self.assertNotIn("file(s) changed", total)
            self.assertNotIn("insertions", total)

    def test_historical_fallback_korean(self):
        src = (ROOT / "src" / "openmainty" / "serve.py").read_text(
            encoding="utf-8")
        self.assertIn("(과거 기록)", src)
        self.assertNotIn("file(s) changed", src)

    @staticmethod
    def _git(work, *args):
        proc = subprocess.run(["git", "-C", str(work)] + list(args),
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return proc.stdout.strip()


if __name__ == "__main__":
    unittest.main()
