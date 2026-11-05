"""Focused UI-logic regressions for work-evidence discovery (dashboard.html).

The dashboard's pure evidence helpers are extracted from the shipped
``src/openmainty/dashboard.html`` inline script and executed with node, so the
test verifies actual logic rather than mirroring static strings:

- explicitly REGISTERED artifact log/JSON files (supplemental build log,
  receipt, cleanup record) and real verification-evidence log/JSON get
  meaningful Korean labels and are surfacable;
- unregistered files (run.json, hidden files), missing files, and
  never-recorded names are never surfaced;
- one physical file keeps one label (human alias wins, collisions get the
  filename suffix);
- historical snapshots stay separated from current documents;
- labels never claim device/native execution.
"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = [
    "dedupeEvidence",
    "artifactLabelFor",
    "evidenceLabelFor",
    "flattenEvidence",
    "isVerEvidenceFile",
    "isHistoricalName",
    "historicalDraftLabelFor",
    "groupEvidenceFiles",
    "uniqueEvidenceLabels",
]

BLOCK_CONSTS = ["ART_LABEL", "SUPP_LABEL", "REVIEW_FILE_LABEL", "STEP_KO"]

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

const stubs = `
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
`;

const tests = `
;(async () => {
// 1) registered supplemental log/JSON get meaningful Korean labels
eq(artifactLabelFor("supplemental_web_build_log"), "추가 빌드 로그 보기", "supp log artifact label");
eq(artifactLabelFor("supplemental_build_receipt"), "추가 빌드 영수증 보기", "supp receipt artifact label");
eq(artifactLabelFor("supplemental_build_cleanup_receipt"), "추가 빌드 정리 기록 보기", "supp cleanup artifact label");
eq(evidenceLabelFor("supplemental-web-build.log.txt"), "추가 빌드 로그", "supp log file label");
eq(evidenceLabelFor("supplemental-build-receipt.json"), "추가 빌드 영수증", "supp receipt file label");
eq(evidenceLabelFor("supplemental-build-cleanup-receipt.json"), "추가 빌드 정리 기록", "supp cleanup file label");
for (const lb of [artifactLabelFor("supplemental_web_build_log"), evidenceLabelFor("supplemental-web-build.log.txt"),
    evidenceLabelFor("supplemental-build-receipt.json"), evidenceLabelFor("supplemental-build-cleanup-receipt.json")]) {
  ok(lb.indexOf("5ba27aee") === -1, "no run id in label");
  ok(lb.indexOf(".log.txt") === -1 && lb.indexOf(".json") === -1, "no raw filename in label: " + lb);
  ok(/추가|검증|검토|실행|기록|보기/.test(lb), "label is meaningful Korean: " + lb);
  ok(lb.indexOf("기기") === -1 && lb.indexOf("네이티브") === -1, "no device claim in label: " + lb);
}

// 2) nested verification evidence flattens; gate admits only recorded+stored log/JSON
eq(JSON.stringify(flattenEvidence([["e2e.log.txt", "supplemental-web-build.log.txt"], "x", null, "  "])),
  JSON.stringify(["e2e.log.txt", "supplemental-web-build.log.txt", "x"]), "nested evidence flattens, empties dropped");
const ver = [["e2e.log.txt", "supplemental-web-build.log.txt", "supplemental-build-receipt.json",
  "supplemental-build-cleanup-receipt.json", "review.scope.json", "review.diff.txt", "review.reviewer.log.txt"]];
const stored = ["e2e.log.txt", "supplemental-web-build.log.txt", "supplemental-build-receipt.json",
  "supplemental-build-cleanup-receipt.json", "review.scope.json", "review.diff.txt", "review.reviewer.log.txt",
  "run.json", "plan.md", "discord.md"];
for (const nm of ["supplemental-web-build.log.txt", "supplemental-build-receipt.json",
    "supplemental-build-cleanup-receipt.json", "e2e.log.txt", "review.scope.json", "review.diff.txt"]) {
  ok(isVerEvidenceFile(nm, ver, stored), "recorded+stored evidence surfacable: " + nm);
}
ok(!isVerEvidenceFile("run.json", ["run.json"], ["run.json"]), "run.json never surfaced");
ok(!isVerEvidenceFile(".lock.log.txt", [".lock.log.txt"], [".lock.log.txt"]), "hidden files never surfaced");
ok(!isVerEvidenceFile("other.log.txt", ver, stored.concat(["other.log.txt"])), "unrecorded log never surfaced");
ok(!isVerEvidenceFile("supplemental-web-build.log.txt", ver, ["plan.md"]), "missing file never surfaced");
ok(!isVerEvidenceFile("notes.txt", ["notes.txt"], ["notes.txt"]), "non log/JSON evidence never surfaced");

// 3) one physical file keeps one label; human alias wins; collisions disambiguated
const dd = dedupeEvidence([{name: "discord.md", label: "discord.md 열기"}],
  [{key: "discord", label: "디스코드 알림 보기", file: "discord.md"}]);
eq(dd.files.length, 1, "one file one entry");
eq(dd.files[0].label, "디스코드 알림 보기", "human alias wins over raw filename");
const dd2 = dedupeEvidence([], [{key: "pr", label: "PR 본문 보기", file: ""}]);
eq(dd2.inline.length, 1, "file-less registered draft stays inline");
eq(dd2.files.length, 0, "no file button invented for inline draft");
const uq = uniqueEvidenceLabels([{file: "a.log.txt", label: "추가 빌드 로그"}, {file: "b.log.txt", label: "추가 빌드 로그"}]);
eq(uq[0].label, "추가 빌드 로그", "first label untouched");
ok(uq[1].label !== "추가 빌드 로그" && uq[1].label.indexOf("b.log.txt") !== -1, "collision gains filename");

// 4) history separated from current docs; ver-listed docs stay current
const mixed = [
  {file: "plan.md", label: "계획서 보기"},
  {file: "pr.historical-20261018T024720.md", label: "PR 본문 (과거 기록)"},
  {file: "supplemental-web-build.log.txt", label: "추가 빌드 로그 보기"},
  {file: "e2e.log.txt", label: "검증 실행 로그"},
  {file: "release_notes.md", label: "릴리스 노트 보기"},
  {file: "review.diffstat.txt", label: "review 변경 요약"},
];
const g = groupEvidenceFiles(mixed, ver);
eq(g.history.length, 1, "only the historical snapshot is history");
eq(g.history[0].file, "pr.historical-20261018T024720.md", "history keeps the exact file");
ok(g.verify.some((f) => f.file === "supplemental-web-build.log.txt"), "supp log in verify group");
ok(g.verify.some((f) => f.file === "e2e.log.txt"), "e2e log in verify group");
ok(g.current.some((f) => f.file === "plan.md"), "current doc stays current");
ok(g.current.some((f) => f.file === "review.diffstat.txt"), "ver-listed diffstat stays current");
ok(!g.verify.some((f) => f.file === "review.diffstat.txt"), "diffstat not duplicated into verify");
const g2 = groupEvidenceFiles([{file: "release_notes.md", label: "릴리스 노트 보기"}], ver);
eq(g2.current.length, 1, "ver-listed md draft stays current, never verify");

// 5) readable step/review labels, unknown artifact keys hidden, no device claim
eq(evidenceLabelFor("e2e.log.txt"), "검증 실행 로그", "e2e log reads as execution log");
eq(evidenceLabelFor("review.scope.json"), "검토 범위 보기", "review scope labeled");
eq(evidenceLabelFor("review.diff.txt"), "검토 변경분 보기", "review diff labeled");
eq(artifactLabelFor("review_scope"), "검토 범위 보기", "review_scope artifact labeled");
eq(artifactLabelFor("review_diff"), "검토 변경분 보기", "review_diff artifact labeled");
eq(artifactLabelFor("pr_historical"), "PR 본문 (과거 기록)", "historical draft labeled, not raw");
eq(artifactLabelFor("mystery_key"), null, "unknown artifact key hidden");
eq(artifactLabelFor("run.json"), null, "run.json key hidden");
eq(artifactLabelFor(""), null, "empty key hidden");
ok(isHistoricalName("pr.historical-20261018T024720.md"), "historical snapshot detected");
// 6) older known historical snapshot without metadata alias gets Korean primary title, identity unchanged
eq(evidenceLabelFor("pr.historical-20261018T024711.md"), "PR 본문 (과거 기록)", "older historical pr snapshot gets Korean primary title");
ok(groupEvidenceFiles([{file: "pr.historical-20261018T024711.md", label: evidenceLabelFor("pr.historical-20261018T024711.md")}], []).history[0].file === "pr.historical-20261018T024711.md", "older historical keeps exact physical identity");
ok(!isHistoricalName("pr.md"), "current draft is not history");
ok(!isHistoricalName("supplemental-web-build.log.txt"), "verify log is not history");
console.log("PASS " + pass);
})();
`;

const pieces = [stubs];
for (const n of wantBlock) pieces.push(extractConstBlock(n));
for (const n of wantFn) pieces.push(extractFn(n));
pieces.push(tests);
try {
  const p = eval(pieces.join("\n"));
  Promise.resolve(p).then(() => {}, (e) => { console.log("FAIL driver :: " + (e && e.message)); process.exit(1); });
} catch (e) { console.log("FAIL eval :: " + (e && e.message)); process.exit(1); }
'''


def _run_node():
    return subprocess.run(
        ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS),
         ",".join(BLOCK_CONSTS)],
        capture_output=True,
        text=True,
        timeout=60,
    )


class WorkEvidenceScriptTest(unittest.TestCase):
    def test_dashboard_script_syntax(self):
        import re

        blocks = re.findall(r"<script>(.*?)</script>", HTML.read_text(),
                            re.S)
        self.assertEqual(len(blocks), 1, "expected one inline dashboard script")
        proc = subprocess.run(
            ["node", "--check", "/dev/stdin"],
            input=blocks[0],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0,
                         f"dashboard JS syntax error: {proc.stderr}")

    def test_evidence_discovery_ui_logic(self):
        proc = _run_node()
        self.assertEqual(
            proc.returncode, 0, f"node driver failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertNotIn("FAIL", proc.stdout, f"logic failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")

    def test_grouped_controls_markers_present(self):
        src = HTML.read_text(encoding="utf-8")
        for token in ("chg-ev-hist", "현재 문서·초안", "검증 로그·영수증 (읽기 전용)",
                      "과거 기록", "ver-ev-", "btn sm evbtn", "data-evfile",
                      "추가 빌드 로그", "추가 빌드 영수증", "추가 빌드 정리 기록"):
            self.assertIn(token, src, f"dashboard missing {token!r}")
        self.assertNotIn("5ba27aee", src, "dashboard must not hardcode the run id")


if __name__ == "__main__":
    unittest.main()
