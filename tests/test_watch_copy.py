"""Narrow watch-inspector copy regressions (dashboard.html).

The dashboard's pure watch-copy helpers are extracted from the shipped
``src/openmainty/dashboard.html`` inline script and executed with node, so
the test verifies actual logic rather than mirrored strings: synthetic
dep keys never read as GitHub issues, active-watch judgments use the
compact kind (the raw reason renders exactly once inside the watch
disclosure), primary summaries carry no raw disposition key, truncation
always ellipsizes, and overdue next-checks never present as future.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = [
    "isSyntheticDepKey",
    "whyStartFor",
    "decisionFor",
    "ell",
    "nextCheckCopy",
    "dispPrimaryFor",
]

DRIVER = r'''
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
let lib = "";
for (const name of wantFn) lib += extractFn(name) + "\n";
const tests = `
;(function () {
let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
// synthetic dep keys are never issues
eq(isSyntheticDepKey("dep-@babel/core-8.0.7"), true, "dep- key is synthetic");
eq(isSyntheticDepKey("123"), false, "numeric issue is not synthetic");
eq(isSyntheticDepKey(""), false, "empty is not synthetic");
// WHY from actual dep fields, never an invented issue
const why = whyStartFor({dep: "@babel/core", target: "8.0.7", lane: "next",
  issue: "dep-@babel-core-8.0.7"}, {});
eq(why.text, "의존성 업데이트 제안 @babel/core → 8.0.7 (next 레인)", "dep WHY text");
ok(why.text.indexOf("이슈 #dep") === -1, "no invented issue from synthetic key");
eq(why.tech, "dep-@babel-core-8.0.7", "synthetic key kept as tech only");
// unknown trigger invents nothing
const unk = whyStartFor({issue: "dep-thing-1.0.0"}, {});
eq(unk.text, "스캔 항목 요청 (트리거 원문 기록 없음)", "unknown trigger fallback");
eq(unk.tech, "dep-thing-1.0.0", "synthetic key as tech");
eq(whyStartFor({}, {}).text, "스캔 항목 또는 이슈 요청", "bare fallback");
// numeric issues stay secondary with real context
const num = whyStartFor({issue: "123"}, {});
ok(num.text.indexOf("#123") !== -1, "numeric issue context kept");
eq(num.tech, "", "numeric issue needs no tech key");
// active-watch judgment is the compact structured kind, never the raw
// 160+ char reason (the original reason lives once in the disclosure)
eq(decisionFor({}, {}, {reason: "wait for compatible peer toolchain"}),
  "외부 대기", "watch judgment is the compact kind");
eq(decisionFor({}, {}, {}), "", "no decision stays empty, never invented");
eq(decisionFor({decision: "hold"}, {}, {reason: "watch reason"}), "hold",
  "explicit decision wins");
// primary disposition carries no raw key
eq(dispPrimaryFor({kind: "EXTERNAL-WATCH", detail: "long wall",
  ref: "repo+@babel/core+8.0.7+next"}), "처리 경위 — 외부 대기", "primary kind only");
eq(dispPrimaryFor(null), "", "no disposition, no line");
// truncation ellipsizes; disclosure keeps full text by construction
eq(ell("abcdef", 4), "abcd…", "cut gets ellipsis");
eq(ell("abc", 4), "abc", "short text untouched");
// overdue checks never present as future
const now = Date.parse("2026-10-17T07:39:00Z");
eq(nextCheckCopy("2026-10-17T07:47:00Z", "", now).kind, "scheduled", "future recorded stays scheduled");
const per = nextCheckCopy("2026-10-17T07:26:00Z", "2026-10-17T07:47:00Z", now);
eq(per.kind, "periodic", "past recorded yields periodic scan");
eq(per.recordedPast, true, "recorded past flagged");
eq(nextCheckCopy("2026-10-17T07:26:00Z", "2026-10-17T07:20:00Z", now).kind,
  "waiting", "all past means waiting, never future");
eq(nextCheckCopy("2026-10-17T07:26:00Z", "", now).kind, "waiting", "past alone never scheduled");
eq(nextCheckCopy("", "", now).kind, "unknown", "nothing known stays unknown");
console.log("PASS " + pass);
})();
`;
eval(lib + tests);
'''

PRIMARY_SECTION = " : `<section><h2>요약</h2>`"


class WatchCopyTests(unittest.TestCase):
    def _run_driver(self):
        proc = subprocess.run(
            ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS)],
            capture_output=True, text=True, timeout=60)
        return proc

    def test_helpers_pass_node_assertions(self):
        proc = self._run_driver()
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_primary_summary_carries_no_raw_disposition_key(self):
        text = HTML.read_text(encoding="utf-8")
        idx = text.find(PRIMARY_SECTION)
        self.assertNotEqual(idx, -1)
        # Normal-path primary ends at the changed-content section.
        primary = text[idx:text.find('id="chg-sec"', idx)]
        self.assertIn("dispShort", primary)
        # Raw disposition detail/ref is defined once and used only
        # inside the collapsed active-watch technical disclosure
        # (watchTech), never as a top-level summary line.
        defn = text.find("const dispLine = disp")
        self.assertNotEqual(defn, -1)
        tech_start = text.find("const watchTech")
        self.assertNotEqual(tech_start, -1)
        tech_end = text.find("`</details>` : \"\"", tech_start)
        self.assertNotEqual(tech_end, -1)
        # The settled-record branch shows the same raw line, also only
        # inside its collapsed 판정 근거/과거 기록 disclosure.
        settled_start = text.find('id="settled-record"')
        self.assertNotEqual(settled_start, -1)
        settled_end = text.find("</details>", settled_start)
        self.assertNotEqual(settled_end, -1)
        for m in re.finditer("dispLine", text):
            if m.start() == defn + len("const "):
                continue
            in_tech = tech_start < m.start() < tech_end
            in_settled = settled_start < m.start() < settled_end
            self.assertTrue(in_tech or in_settled,
                            "raw disposition must only surface via watchTech "
                            "or the settled-record disclosure")
        self.assertNotIn("참조 ${", primary)

    def test_watch_reason_appears_once_in_primary_disclosure(self):
        text = HTML.read_text(encoding="utf-8")
        # The watch summary block (const definition through the rendered
        # primary section) carries the raw reason exactly once, inside
        # the existing watch-record disclosure; the top summary uses
        # only the compact kind copy.
        start = text.find("const watchHead")
        self.assertNotEqual(start, -1)
        end = text.find('id="chg-sec"', start)
        self.assertNotEqual(end, -1)
        block = text[start:end]
        self.assertEqual(block.count("watchRec.reason"), 1,
                         "raw watch reason must appear once (disclosure)")
        self.assertIn('id="watch-record"', block)
        self.assertNotIn("ell(watchRec.reason", text)

    def test_active_watch_summary_has_single_status_line(self):
        text = HTML.read_text(encoding="utf-8")
        # The active-watch branch renders one short panel: the status
        # head, the purpose line, the single watch disclosure, the next
        # action, and the folded technical disclosure. Judgment/result/
        # classification/promotion-wall repeats must not appear there.
        start = text.find("watchRec ? `<section><h2>요약</h2>`")
        self.assertNotEqual(start, -1)
        end = text.find("</section>", start)
        self.assertNotEqual(end, -1)
        branch = text[start:end]
        self.assertIn("watchHead", branch)
        self.assertIn("watchDetails", branch)
        self.assertIn("watchTech", branch)
        for repeated in ("판단 —", "결과 —", "분류 —", "외부 대기(",
                         "외부 대기 —", "승격 막힘 —", "처리 경위 —"):
            self.assertNotIn(repeated, branch,
                             f"active-watch summary must not repeat: {repeated}")
        # The folded technical disclosure still carries the evidence.
        tech_start = text.find("const watchTech")
        self.assertNotEqual(tech_start, -1)
        tech = text[tech_start:text.find('id="chg-sec"', tech_start)]
        self.assertIn("dispLine", tech)
        self.assertIn("분류 —", tech)
        self.assertIn("승격 조건 원문", tech)


if __name__ == "__main__":
    unittest.main()
