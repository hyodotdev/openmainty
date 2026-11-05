"""Latest-run fastcheck summary rendered through the REAL work report.

The shipped ``fcReportHtml`` (plus its small helpers) is extracted
from ``src/openmainty/dashboard.html`` and executed with the real
``esc`` and canned ``fastcheck_report`` projections, so the test
verifies the actual rendered heading, state chips, warning lines,
accordion semantics and escaping — not a copy of the logic.
Run with: python3 -m unittest tests.test_work_fastcheck_report_ui
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = ["fcRepChip", "fcRepIso", "fcRepRc", "fcRepTail",
             "fcRepSteps", "fcRepRow", "fcRepConsumer",
             "fcReportHtml"]


def _real_esc_source():
    for line in HTML.read_text(encoding="utf-8").splitlines():
        if line.startswith("function esc("):
            return line + "\n"
    raise AssertionError("real esc not found")


DRIVER_HEAD = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
const wantFn = process.argv[2].split(",");
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
let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) {
  if (c) { pass++; }
  else { console.log("FAIL " + msg); process.exitCode = 1; }
}
'''

DRIVER_TESTS = '''
;(async () => {
const H40 = "a".repeat(40), F64 = "b".repeat(64);
/* Real producer shape: Turbo substeps carry argv/rc/tails/isolation
   but no name; labels derive from the command shape. */
const turboSteps = [
  {label: "버전 확인", argv: "turbo --version", rc: 0, timed_out: false,
   elapsed_s: 0.3, tail: "2.11.10\\n", truncated: false,
   isolation: {network: false, install: false, profile: "p", files: "f",
     writes: [], writes_omitted: 0, children_gone: true,
     log_truncated: false, log_bytes: 200, log_sha256: "d".repeat(64)}},
  {label: "실제 실행(force)", argv: "turbo run build:web --force", rc: 0,
   timed_out: false, elapsed_s: 11.0,
   tail: 'warning "example#build:web" no output files found\\nCompiled ok\\n',
   truncated: true,
   isolation: {network: false, install: false, profile: "p", files: null,
     writes: ["/tmp/x"], writes_omitted: 2, children_gone: true,
     log_truncated: true, log_bytes: 900, log_sha256: "e".repeat(64)}}];
const PASS = {state: "pass-worker", reason: "앱 자동 검사 통과 · 최종 판단은 별도 기록 참조",
  op: "prep-run-5720a20b", recorded: {head: H40, fingerprint: F64, path: "/wt"},
  live: {head: H40, fingerprint: F64, clean: true, worktree: "/wt"},
  hold: {state: "held", by: "root",
    reason: "only CIcons, no actual turbo executed", at: "2026-10-19T00:00:00"},
  pending_gates: {count: 2, labels: ["최종 검수 확인", "보류 결정"],
    by_status: {fail: 0, blocked: 0, pending: 2, other: 0}},
  receipt_sha256: "f".repeat(64), at: "2026-10-25T17:37:47",
  wall_s: 66.8, worker_attested: true, root_attested: false, ok: true,
  consumer: {status: "pass", turbo: true, script: "Turbo Web (turbo)",
    label: "Turbo Web 작업 실행 검증 통과", manager: "yarn", name: "c",
    rc: 0, timed_out: false, elapsed_s: 11.7,
    reason: "", note: "Android/네이티브 CI 미검증 — Web 소비 검증만 해당",
    turbo_version_expected: "2.11.10", turbo_version_actual: "2.11.10",
    turbo_task_count: 1, turbo_scope: "example",
    turbo_tasks: ["example#build:web"], turbo_steps: turboSteps},
  steps: [{name: "unit", gate: "unit test", rc: 0, timed_out: false,
    elapsed_s: 60.1, argv: "yarn test", head: H40, fingerprint: F64,
    tail: "pass\\n", truncated: false,
    isolation: {network: "loopback-only", install: null, profile: "p",
      files: null, writes: [], writes_omitted: 0, children_gone: true,
      log_truncated: false, log_bytes: 10, log_sha256: null}}],
  warnings: [{step: "consumer", line: 'warning "example#build:web" no output files found'}],
  warnings_omitted: 2};

// 1) pass-worker: heading, chip, facts, warnings, hold line.
const h1 = fcReportHtml(PASS);
ok(h1.indexOf("준비한 코드의 검사 결과") >= 0, "pass heading present");
ok(h1.indexOf("통과 · 앱 자동 검사") >= 0, "pass chip present");
ok(h1.indexOf("자동 검사 기록 · 최종 판단은 별도") >= 0, "review separation shown");
ok(h1.indexOf("독립 검수됨") < 0, "no false root claim from worker boolean");
ok(h1.indexOf("시작(기록 생성)") >= 0, "start labelled as record creation");
ok(h1.indexOf("2.11.10") >= 0, "turbo version shown");
ok(h1.indexOf("기대와 일치") >= 0, "version match labelled");
ok(h1.indexOf("!=") < 0 && h1.indexOf("==") < 0, "no raw formulas");
ok(h1.indexOf("Web 작업 1개") >= 0, "task count shown");
ok(h1.indexOf("남은 검증·검토 2건") >= 0, "pending count shown accurately");
ok(h1.indexOf("승인 대기") < 0, "no approval-waiting mislabel");
ok(h1.indexOf("최종 검수 확인") >= 0, "pending labels shown");
ok(h1.indexOf("보류 결정 — 유지 중") >= 0, "actual hold kept active");
ok(h1.indexOf("only CIcons, no actual turbo executed") >= 0, "hold text verbatim");
ok(h1.indexOf("2026-10-19") >= 0, "hold dated from hold record");
ok(h1.indexOf("등록된 결정을 원문 그대로 둔다") >= 0, "hold not rewritten");
// No hold on the batch: the maintained-hold disclosure stays absent
// even though pending gate labels are still shown; the grid says none.
const NOHOLD = Object.assign({}, PASS, {hold: {state: "none"}});
const hnh = fcReportHtml(NOHOLD);
ok(hnh.indexOf("보류 결정 — 유지 중") < 0, "no invented hold");
ok(hnh.indexOf("최종 검수 확인") >= 0, "pending labels kept");
ok(hnh.indexOf("<dt>보류</dt><dd>없음</dd>") >= 0, "no-hold grid cell");
// Minimal server-shaped unknown: no pending count invented.
const MINUNK = {state: "unknown", reason: "기록을 읽지 못함"};
const hmu = fcReportHtml(MINUNK);
ok(hmu.indexOf("미확인") >= 0, "minimal unknown shown");
ok(hmu.indexOf("남은 검증·검토 미확인") >= 0, "missing count not zero");
ok(hmu.indexOf("0건") < 0, "no invented zero count");
ok(hmu.indexOf("통과 · 앱 자동 검사") < 0, "minimal unknown never passes");
ok(hmu.indexOf("<dt>보류</dt><dd>미확인</dd>") >= 0, "unknown hold in grid");
// Unknown hold: explicit unknown, never a maintained claim.
const UNKHOLD = Object.assign({}, PASS, {hold: {state: "unknown"}});
const huh = fcReportHtml(UNKHOLD);
ok(huh.indexOf("<dt>보류</dt><dd>미확인</dd>") >= 0, "unknown hold explicit in grid");
ok(huh.indexOf("보류 결정 — 유지 중") < 0, "unknown never maintained");
// Count 10 with only 6 labels projected: omitted derives from the
// known total, not from the label list length.
const BIG = Object.assign({}, PASS, {
  pending_gates: {count: 10,
    labels: ["L1", "L2", "L3", "L4", "L5", "L6"]}});
const hb = fcReportHtml(BIG);
ok(hb.indexOf("남은 검증·검토 10건") >= 0, "known total shown");
ok(hb.indexOf("외 7건") >= 0, "omitted from total minus shown");
ok(hb.indexOf("외 3건") < 0, "no label-count-derived count");
ok(hb.indexOf("외 4건") < 0, "no projected-list-derived count");
// Truncated hold reason carries an explicit cue instead of a
// verbatim claim.
const TRUNCHOLD = Object.assign({}, PASS, {hold: {state: "held",
  by: "root", reason: "긴 사유", reason_truncated: true,
  at: "2026-10-19T00:00:00"}});
ok(fcReportHtml(TRUNCHOLD).indexOf("보류 사유 일부 잘림") >= 0,
  "truncation cue shown");
ok(h1.indexOf("원문 그대로") >= 0, "short hold keeps verbatim claim");
ok(h1.indexOf("외 2건 숨김") >= 0, "omitted warnings counted");
ok(h1.indexOf("원인 미확정") >= 0, "warning cause not invented");
ok(h1.indexOf("no output files found") >= 0, "outputs warning visible");
ok(h1.indexOf("경고 원문 보기(1건)") >= 0, "full warnings folded");
ok(h1.indexOf("Android/네이티브 CI 미검증") >= 0, "native scope explicit");
ok(h1.indexOf('role="status"') >= 0, "state has status role");
ok((h1.match(/<details/g) || []).length >= 3, "accordions for steps/turbo/provenance");
ok((h1.match(/<summary/g) || []).length >= 3, "accordion summaries present");
ok(h1.indexOf("onclick") < 0, "no new mutation actions");
ok(h1.indexOf("Turbo 실행 단계 2건") >= 0, "turbo substep count");
ok(h1.indexOf("로그 일부 잘림") >= 0, "truncation flagged");
// Scrollable tails stay keyboard reachable with a named region.
ok(h1.indexOf('tabindex="0"') >= 0, "log regions take a tab stop");
ok(h1.indexOf('role="region"') >= 0, "log regions expose region semantics");
ok(h1.indexOf("실제 실행(force) 로그") >= 0, "log region named by step");
ok(h1.indexOf("unit 로그") >= 0, "plain step log named");
ok(h1.indexOf('class="fcrep"') >= 0, "single report card");
ok(h1.indexOf("fcrep-grid") >= 0, "fact grid present");
ok(h1.indexOf("<dt>범위</dt>") >= 0, "scope row present");
ok(h1.indexOf("<dt>대기</dt>") >= 0, "pending row present");
ok(h1.indexOf("<dt>판단</dt>") >= 0, "review row present");
ok(h1.indexOf("<dt>보류</dt><dd>유지 중 · 2026-10-19 · root</dd>") >= 0, "held state in grid");
// Full identities stay reachable in the folded provenance details,
// not in the compact primary prose.
ok(h1.indexOf(H40) >= 0, "full head reachable in provenance");
ok(h1.indexOf("f".repeat(64)) >= 0, "full sha reachable in provenance");
ok(h1.indexOf("기록 출처 보기") >= 0, "provenance folded");

// 2) other states never claim a pass.
const STALE = Object.assign({}, PASS, {state: "stale", reason: "현재 원본이 기록과 다름"});
const hs = fcReportHtml(STALE);
ok(hs.indexOf("기록 · 현재 확인 필요") >= 0, "stale chip present");
ok(hs.indexOf("통과 · 앱 자동 검사") < 0, "stale never passes");
const UNK = Object.assign({}, PASS, {state: "unknown", reason: "기록을 읽지 못함"});
const hu = fcReportHtml(UNK);
ok(hu.indexOf("미확인") >= 0, "unknown chip present");
ok(hu.indexOf("통과 · 앱 자동 검사") < 0, "unknown never passes");
const MISS = {state: "missing", reason: "검사 기록 없음"};
const hm = fcReportHtml(MISS);
ok(hm.indexOf("검사 기록 없음") >= 0, "missing shown");
ok(hm.indexOf("통과 · 앱 자동 검사") < 0, "missing never passes");
ok(fcReportHtml(null) === "", "null report renders nothing");
ok(hc(hm) < 0, "missing has no accordions");
function hc(s) { return s.indexOf("<details"); }

// 3) fail-worker: failed point shown, no pass claim.
const FAIL = Object.assign({}, PASS, {state: "fail-worker",
  reason: "자동 검사 실패",
  failed: {argv: "turbo run build:web", rc: 1, timed_out: false}});
const hf = fcReportHtml(FAIL);
ok(hf.indexOf("실패 · 앱 자동 검사") >= 0, "fail chip present");
ok(hf.indexOf("실패 지점") >= 0 && hf.indexOf("turbo run build:web") >= 0, "failed argv shown");
ok(hf.indexOf("통과 · 앱 자동 검사") < 0, "fail never passes");
// Historical failure on drifted source: outcome kept, not current-code failure.
const HIST = Object.assign({}, FAIL, {historical: true,
  reason: "자동 검사 실패(과거 기록 · 현재 원본과 다름)"});
const hh = fcReportHtml(HIST);
ok(hh.indexOf("과거 실패 기록") >= 0, "historical fail labelled");
ok(hh.indexOf("현재 코드 실패 아님") >= 0, "historical not current failure");
ok(hh.indexOf("통과 · 앱 자동 검사") < 0, "historical never passes");
// Worker booleans never become a review verdict on their own: even
// root attested plus zero pending gates stays neutral here; the
// current-QA section owns that judgment.
const DONE = Object.assign({}, PASS, {root_attested: true,
  pending_gates: {count: 0, labels: []}});
const hd = fcReportHtml(DONE);
ok(hd.indexOf("독립 검수됨") < 0, "worker report stays neutral");
ok(hd.indexOf("자동 검사 기록 · 최종 판단은 별도") >= 0, "neutral review line");
// Command-shape labels render even without recorded names.
ok(h1.indexOf("버전 확인") >= 0, "version label shown");
ok(h1.indexOf("실제 실행(force)") >= 0, "run label shown");

// 4) generic consumer: no turbo block, argv/isolation rendered.
const GEN = Object.assign({}, PASS, {consumer: {status: "pass", turbo: false,
  script: "webpack build", label: "소비 검증 통과", manager: "yarn",
  name: "consumer (webpack)", rc: 0, timed_out: false, elapsed_s: 5.0,
  reason: "", note: "",
  argv: "yarn workspace example build", tail: "asset bundle.js\\n",
  truncated: false,
  isolation: {network: "loopback-only", install: null, profile: "p",
    files: null, writes: [], writes_omitted: 0, children_gone: true,
    log_truncated: false, log_bytes: 50, log_sha256: null}}});
const hg = fcReportHtml(GEN);
ok(hg.indexOf("Turbo 실행 단계") < 0, "generic has no turbo block");
ok(hg.indexOf("yarn workspace example build") >= 0, "generic argv shown");

// 5) escaping with the real esc: hostile markup never raw.
const EVIL = Object.assign({}, PASS, {steps: [{name: "<script>alert(1)</script>",
  gate: "g", rc: 0, timed_out: false, elapsed_s: 1.0,
  argv: "run <img src=x onerror=y>", head: H40, fingerprint: F64,
  tail: "x < y\\n", truncated: false, isolation: null}]});
const he = fcReportHtml(EVIL);
ok(he.indexOf("<script>alert") < 0, "script tag escaped");
ok(he.indexOf("&lt;script&gt;") >= 0, "script tag entity present");
ok(he.indexOf("<img") < 0, "img escaped");

// 6) install isolation wording: explicit allow, never blocked-by-default,
// and no invented file isolation without file proof.
const isoHtml = fcRepIso({network: false, install: true, profile: "p",
  files: "f", writes: [], writes_omitted: 0, children_gone: true,
  log_truncated: false, log_bytes: 1, log_sha256: null});
ok(isoHtml.indexOf("네트워크 허용(설치)") >= 0, "install allow shown");
ok(isoHtml.indexOf("파일 격리") >= 0, "install file proof shown");
ok(isoHtml.indexOf("차단") < 0, "install never blocked-by-default");
const isoNoFile = fcRepIso({network: false, install: true, profile: null,
  files: null, writes: [], writes_omitted: 0, children_gone: null,
  log_truncated: false, log_bytes: 1, log_sha256: null});
ok(isoNoFile.indexOf("파일 격리 미기록") >= 0, "no invented file isolation");
ok(fcRepIso(null) === "격리 미기록", "missing isolation unknown");
ok(fcRepIso({network: "loopback-only", install: null, profile: null,
  files: null, writes: null, writes_omitted: 0, children_gone: null,
  log_truncated: false, log_bytes: null, log_sha256: null}
  ).indexOf("차단(로컬 루프백만 허용)") >= 0, "recorded network mapped");
})();
'''


class WorkFastcheckReportUiTests(unittest.TestCase):
    def run_driver(self):
        driver = (DRIVER_HEAD + _real_esc_source() + ";\n"
                  + "const __fns = {};\n"
                  + "Promise.resolve((function(){\n"
                  + "  const defs = wantFn.map(extractFn).join(\"\\n\");\n"
                  + "  return eval(defs + \"\\n\" + "
                  + repr(DRIVER_TESTS)
                  + ");\n})())\n"
                  + ".then(() => console.log(\"PASS \" + pass),\n"
                  + " (e) => { console.log(\"FAIL driver \" "
                  + "+ (e && e.stack || e)); process.exitCode = 1; });\n")
        proc = subprocess.run(
            ["node", "-e", driver, str(HTML), ",".join(FUNCTIONS)],
            capture_output=True, text=True, timeout=120)
        return proc

    def test_rendered_fastcheck_summary(self):
        proc = self.run_driver()
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout[-2000:]}"
                         f"{proc.stderr[-2000:]}")
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_dashboard_script_syntax(self):
        import tempfile
        html = HTML.read_text(encoding="utf-8")
        start = html.index("<script>")
        end = html.index("</script>")
        inline = html[start + len("<script>"):end]
        with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False,
                encoding="utf-8") as fh:
            fh.write(inline)
            name = fh.name
        proc = subprocess.run(
            ["node", "--check", name],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"node --check failed: {proc.stderr[-2000:]}")


if __name__ == "__main__":
    unittest.main()
