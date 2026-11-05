"""UI237 gate (3): 44px targets, AA contrast, graph loading honesty,
390px calm, add-repo title sync.

Static tests fail on the old markup (they assert the exact new rules,
strings and call sites), and node-driver tests run the shipped
setGraphMsg/syncAddGo against stub DOM. Reads the shipped
dashboard.html only; no server, no network, no HOME needed.
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent
        / "src" / "openmainty" / "dashboard.html").read_text(
            encoding="utf-8")
TAIL = HTML[HTML.index("/* UI237 gate (3)"):]
STYLE = re.findall(r"<style>(.*?)</style>", HTML, re.S)[0]


def _balanced_js(source, prefix):
    lines = source.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(prefix))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i] == "}\n")
    return "".join(lines[start:end + 1])


def _run_node(program):
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                     delete=False,
                                     encoding="utf-8") as fh:
        fh.write(program)
        name = fh.name
    try:
        proc = subprocess.run(["node", name], capture_output=True,
                              text=True, timeout=120)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr[-2000:]}")
    return [json.loads(line) for line in proc.stdout.splitlines()
            if line.strip()]


def _rel_lum(hexcolor):
    h = hexcolor.strip().lstrip("#")
    if len(h) == 8:
        h = h[:6]
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(fg, bg):
    a, b = _rel_lum(fg), _rel_lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _theme_vars():
    light = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);",
                            re.search(r":root\s*\{(.*?)\}", HTML,
                                      re.S).group(1)))
    dark_raw = re.search(r'html\[data-theme="dark"\]\s*\{(.*?)\n\}',
                         HTML, re.S).group(1)
    dark = dict(light)
    dark.update(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", dark_raw))
    for table in (light, dark):
        for key, value in list(table.items()):
            while value.strip().startswith("var("):
                value = table[value.strip()[4:-1].strip()]
            table[key] = value.strip()[:7]
    return light, dark


class StaticRulesTests(unittest.TestCase):
    # Every snippet below exists only in the UI237 block (or the new
    # call sites), so each test fails on the old markup.

    def test_u1_summary_heads(self):
        self.assertIn("details > summary { min-height: 44px; }", TAIL)

    def test_u1_buttons(self):
        self.assertIn(".btn, a.btn { min-height: 44px; min-width: 44px;",
                      TAIL)

    def test_u1_header_skip_inputs(self):
        self.assertIn(".kbtn { min-width: 44px;", TAIL)
        self.assertIn(".skip { min-height: 44px; }", TAIL)
        self.assertIn(
            "input[type=text], input[type=search], select { min-height: 44px; }",
            TAIL)

    def test_r1_no_global_anchor_sizing(self):
        # The claude237 broad rule is gone: no selector may put
        # min-height or a non-inline display on plain in-flow links.
        self.assertNotIn("#view a:not(.btn)", HTML)
        self.assertNotIn(".sheet a:not(.btn)", HTML)
        self.assertNotIn(".pop a:not(.btn)", HTML)

    def test_r1_standalone_action_links(self):
        self.assertIn(".alink { display: inline-flex;", TAIL)
        self.assertIn("min-height: 44px;\n  min-width: 44px; }", TAIL)
        # Every standalone action anchor carries the class: the repo
        # line, the palette empty-state link, and all five issue links.
        self.assertIn('<a class="alink" href="#/repo/', HTML)
        self.assertIn('<a class="alink" href="#" id="pal-add">', HTML)
        bare = len(re.findall(r'<a href="\$\{[^}]*\}" target="_blank" '
                                r'rel="noopener">이슈 ↗</a>', HTML))
        classed = len(re.findall(r'class="alink[^"]*" href="[^"]*" '
                                 r'target="_blank" rel="noopener">이슈 ↗</a>',
                                 HTML))
        self.assertEqual(bare, 0)
        # Four bare-pattern issue links plus the muted-small one,
        # all carrying alink now (pal-add asserted separately above).
        # 291 adds one more: the grouped recent-event row carries its
        # own compliant 이슈 link next to eventHtml's.
        self.assertEqual(classed, 6)

    def test_r3_repo_slug_link_carries_alink(self):
        # #/repo/<name> standalone slug link is its own line (not
        # inside a sentence), so it carries alink for the 44px target.
        self.assertIn(
            '<a class="alink" '
            'href="https://github.com/${esc(r.github)}"', HTML)

    def test_u1_rows_crumbs_chips_evidence(self):
        for snippet in (".wrowlink { min-height: 44px;",
                        ".crumb { min-height: 44px;",
                        ".frow { min-height: 44px; }",
                        ".prow { min-height: 44px; flex-wrap: wrap; }",
                        ".chip { min-height: 44px;",
                        "a.evlink { min-height: 44px; }"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, TAIL)
        self.assertIn('<span class="plab">', HTML)
        self.assertIn("keep-all", TAIL)

    def test_u1_small_controls(self):
        self.assertIn("label.srcpick { min-height: 44px;", TAIL)
        self.assertIn("#watch-edit label.wp { min-height: 44px; }", TAIL)
        self.assertIn(".sendbtn { width: 44px; height: 44px; }", TAIL)

    def test_u2_legend_off_keeps_text(self):
        self.assertIn(
            '.legend button[aria-pressed="false"] { opacity: 1;', TAIL)
        self.assertIn("border-style: dashed;", TAIL)

    def test_u2_dim_shapes_not_labels(self):
        self.assertIn("#gsvg .nd.dim { opacity: 1; }", TAIL)
        self.assertIn("#gsvg .nd.dim > circle { opacity: .25; }", TAIL)
        self.assertIn("#gsvg .nd .cnt { fill: #101828; }", TAIL)
        self.assertIn('html[data-theme="dark"] #gsvg .nd .cnt', TAIL)

    def test_u3_canvas_message(self):
        self.assertIn('id="gmsg" role="status"', HTML)
        self.assertIn("function setGraphMsg(text, busy)", HTML)
        self.assertIn("aria-busy", HTML)
        self.assertIn("관계 그래프를 불러오는 중…", HTML)
        self.assertIn("그래프를 불러오지 못했습니다: ", HTML)
        self.assertIn("표시할 노드가 없습니다", HTML)

    def test_u4_toast_and_palette(self):
        tail = HTML[HTML.index("/* U4: 토스트는 390px에서도"):]
        self.assertIn("bottom: 16px", tail)
        self.assertIn(".pfoot span { white-space: nowrap;", TAIL)

    def test_u5_title_sync(self):
        self.assertIn("function syncAddGo()", HTML)
        self.assertIn("syncAddGo();", HTML)
        self.assertIn("git 저장소 폴더를 여세요", HTML)
        self.assertIn("추가${target", HTML)


class ContrastTests(unittest.TestCase):
    # Every solid text/background token pair used for small text,
    # in both themes, computed from the shipped custom properties.
    PAIRS = [("ink", "panel"), ("ink2", "panel"), ("ink3", "panel"),
             ("ink3", "bg"), ("ink3", "side"), ("ink2", "side"),
             ("accent", "panel"), ("accent-ink", "accent"),
             ("ok-ink", "panel"), ("warn-ink", "panel"),
             ("bad-ink", "panel"), ("run-ink", "panel")]

    def test_all_text_pairs_meet_aa(self):
        light, dark = _theme_vars()
        for theme_name, table in (("light", light), ("dark", dark)):
            for fg_key, bg_key in self.PAIRS:
                with self.subTest(theme=theme_name, fg=fg_key,
                                  bg=bg_key):
                    self.assertGreaterEqual(
                        _ratio(table["--" + fg_key],
                               table["--" + bg_key]), 4.5)

    def test_dimmed_text_keeps_aa(self):
        # The legend off-state no longer dims (see static test);
        # pending (.75) and evidence filenames (.65) still compute AA.
        light, dark = _theme_vars()
        for theme_name, table in (("light", light), ("dark", dark)):
            ink, panel = table["--ink2"], table["--panel"]
            with self.subTest(theme=theme_name, state="legend-off"):
                self.assertGreaterEqual(_ratio(ink, panel), 4.5)


class NodeBehaviorTests(unittest.TestCase):
    def test_graph_msg_loading_error_empty(self):
        program = (r"""
let __el = null;
function $(id) { return __el; }
""" + _balanced_js(HTML, "function setGraphMsg(") + r"""
const seen = [];
function snap() {
  seen.push({text: __el.textContent, hidden: __el.classList.contains("hidden"),
    busy: __el.getAttribute("aria-busy")});
}
function mk() {
  const cls = new Set(["hidden"]);
  const attrs = {};
  __el = {textContent: "",
    classList: {toggle: (c, f) => { f ? cls.add(c) : cls.delete(c); },
      contains: (c) => cls.has(c)},
    setAttribute: (k, v) => { attrs[k] = v; },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    removeAttribute: (k) => { delete attrs[k]; }};
}
mk(); setGraphMsg("관계 그래프를 불러오는 중…", true); snap();
setGraphMsg("그래프를 불러오지 못했습니다: boom", false); snap();
setGraphMsg("표시할 노드가 없습니다", false); snap();
setGraphMsg("", false); snap();
console.log(JSON.stringify(seen));
""")
        got = _run_node(program)[0]
        self.assertEqual(got[0]["text"], "관계 그래프를 불러오는 중…")
        self.assertFalse(got[0]["hidden"])
        self.assertEqual(got[0]["busy"], "true")
        self.assertIn("그래프를 불러오지 못했습니다: boom", got[1]["text"])
        self.assertIsNone(got[1]["busy"])
        self.assertIn("표시할 노드가 없습니다", got[2]["text"])
        self.assertTrue(got[3]["hidden"])

    def test_add_go_title_sync(self):
        program = (r"""
let browse = {path: "", is_git: false};
let modalHidden = false, typed = "";
function $(id) {
  if (id === "ar-go") return goEl;
  if (id === "modal") return {classList: {contains: () => modalHidden}};
  if (id === "ar-path") return {value: typed};
  return null;
}
let goEl = {disabled: true, title: ""};
""" + _balanced_js(HTML, "function syncAddGo()") + r"""
const out = [];
browse = {path: "", is_git: false}; typed = "";
goEl = {disabled: true, title: ""}; syncAddGo();
out.push({disabled: goEl.disabled, title: goEl.title});
browse = {path: "", is_git: false}; typed = "/x/y";
goEl = {disabled: true, title: ""}; syncAddGo();
out.push({disabled: goEl.disabled, title: goEl.title});
browse = {path: "/r/co", is_git: true}; typed = "";
goEl = {disabled: true, title: ""}; syncAddGo();
out.push({disabled: goEl.disabled, title: goEl.title});
console.log(JSON.stringify(out));
""")
        got = _run_node(program)[0]
        self.assertTrue(got[0]["disabled"])
        self.assertEqual(got[0]["title"], "git 저장소 폴더를 여세요")
        self.assertFalse(got[1]["disabled"])
        self.assertIn("추가", got[1]["title"])
        self.assertIn("/x/y", got[1]["title"])
        self.assertFalse(got[2]["disabled"])
        self.assertIn("추가", got[2]["title"])


MATCHER_JS = r"""
function stripComments(css) { return css.replace(/\/\*[\s\S]*?\*\//g, ""); }
function splitTopRules(text) {
  const out = []; const re = /([^{}]+)\{([^{}]*)\}/g; let m;
  while ((m = re.exec(text))) out.push({sel: m[1].trim(), body: m[2]});
  return out;
}
function parseRules(css) {
  css = stripComments(css); const rules = [];
  let i = 0; const n = css.length;
  function readBlock() {
    let depth = 0; const start = i; i++;
    while (i < n) {
      if (css[i] === "{") depth++;
      else if (css[i] === "}") { if (!depth) { const t = css.slice(start + 1, i); i++; return t; } depth--; }
      i++;
    }
    return "";
  }
  while (i < n) {
    while (i < n && /\s/.test(css[i])) i++;
    if (i >= n) break;
    if (css.startsWith("@media", i)) {
      const m = css.slice(i).match(/^@media\s*([^{]+)\{/);
      const cond = m[1].trim(); i += m[0].length - 1;
      const inner = readBlock();
      for (const r of splitTopRules(inner)) rules.push({sel: r.sel, body: r.body, media: cond});
    } else if (css[i] === "}") { i++; }
    else {
      const j = css.indexOf("{", i);
      if (j < 0) break;
      const sel = css.slice(i, j).trim(); i = j;
      const body = readBlock();
      if (sel && !sel.startsWith("@")) rules.push({sel, body, media: null});
    }
  }
  return rules;
}
function mediaActive(cond, width) {
  if (!cond) return true;
  for (const m of cond.matchAll(/\(max-width\s*:\s*(\d+)px\)/g)) if (width > +m[1]) return false;
  for (const m of cond.matchAll(/\(min-width\s*:\s*(\d+)px\)/g)) if (width < +m[1]) return false;
  return true;
}
function decls(body) {
  const o = {};
  for (const part of body.split(";")) {
    const k = part.indexOf(":");
    if (k > 0) o[part.slice(0, k).trim()] = part.slice(k + 1).trim();
  }
  return o;
}
function matchCompound(elm, cpd) {
  let s = String(cpd).trim();
  const nots = [];
  s = s.replace(/:not\(([^)]*)\)/g, (m, inner) => { nots.push(inner.trim()); return ""; });
  if (/:(hover|focus|focus-visible|active|visited|link|first-child|last-child|nth-child|nth-of-type)/.test(s)) return false;
  s = s.replace(/::?[a-zA-Z-]+(\([^)]*\))?/g, "");
  const tag = (s.match(/^[a-zA-Z][a-zA-Z0-9-]*/) || [])[0] || null;
  if (tag && tag !== "*" && elm.tag !== tag) return false;
  for (const m of s.matchAll(/#([A-Za-z0-9_-]+)/g)) if (elm.id !== m[1]) return false;
  for (const m of s.matchAll(/\.([A-Za-z0-9_-]+)/g)) if (!elm.classes.includes(m[1])) return false;
  for (const m of s.matchAll(/\[([A-Za-z0-9_-]+)(?:=("[^"]*"|'[^']*'|[^\]]*))?\]/g)) {
    const v = m[2]; const av = elm.attrs[m[1]];
    if (v === undefined) { if (av === undefined) return false; }
    else if (String(av) !== v.replace(/^["']|["']$/g, "")) return false;
  }
  for (const inner of nots) if (matchCompound(elm, inner)) return false;
  return true;
}
function matches(path, selector) {
  for (const group of String(selector).split(",")) {
    const parts = group.trim().split(/\s+/).filter(Boolean);
    if (!parts.length) continue;
    let pi = path.length - 1; let ok = true;
    for (let k = parts.length - 1; k >= 0; k--) {
      let found = false;
      while (pi >= 0) {
        if (matchCompound(path[pi], parts[k])) { found = true; pi--; break; }
        pi--;
      }
      if (!found) { ok = false; break; }
    }
    if (ok) return true;
  }
  return false;
}
function effective(path, rules, width) {
  const out = {};
  for (const r of rules) {
    if (!mediaActive(r.media, width)) continue;
    if (matches(path, r.sel)) Object.assign(out, decls(r.body));
  }
  return out;
}
function E(tag, classes, parent, extra) {
  const elm = {tag, id: "", classes: classes || [], attrs: {}, parent: parent || null};
  if (extra && extra.id) elm.id = extra.id;
  if (extra && extra.attrs) elm.attrs = extra.attrs;
  return elm;
}
"""


class R1DomTests(unittest.TestCase):
    # A paragraph with inline links keeps the same height as one
    # without: nothing in the stylesheet gives plain in-flow links a
    # min-height or a non-inline display, at 1280 or 390. With the same
    # font and line-height, the line boxes are therefore identical.
    # Standalone action links (.alink) still resolve to 44px.

    def _run_case(self, width, tag, classes, attrs):
        program = ("const STYLE = " + json.dumps(STYLE) + ";\n"
                   + MATCHER_JS + r"""
const rules = parseRules(STYLE);
const view = E("div", [], null, {id: "view"});
const p = E("p", ["ev", "small"], view);
const a = E("%TAG%", %CLASSES%, p, {attrs: %ATTRS%});
const path = [view, p, a];
const st = effective(path, rules, %WIDTH%);
console.log(JSON.stringify({display: st["display"] || null,
  minHeight: st["min-height"] || null,
  minWidth: st["min-width"] || null}));
""" .replace("%TAG%", tag)
    .replace("%CLASSES%", json.dumps(classes))
    .replace("%ATTRS%", json.dumps(attrs))
    .replace("%WIDTH%", str(width)))
        return _run_node(program)[0]

    def test_paragraph_links_stay_inline(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                got = self._run_case(width, "a", [], {"href": "#"})
                self.assertIsNone(got["display"])
                self.assertIsNone(got["minHeight"])

    def test_standalone_action_links_keep_44px(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                got = self._run_case(width, "a", ["alink"],
                                     {"href": "#/repo/r"})
                self.assertEqual(got["display"], "inline-flex")
                self.assertEqual(got["minHeight"], "44px")
                self.assertEqual(got["minWidth"], "44px")

    def test_evidence_buttons_keep_44px(self):
        got = self._run_case(1280, "button", ["linkbtn"], {})
        self.assertEqual(got["minHeight"], "44px")

    def test_r2_overrides_win_cascade(self):
        # The 24px floors stay in the file (owner guarantee), but the
        # later same-specificity R2 layer wins: effective size is 44.
        cases = [
            ("button", ["filelink"], {}, {"min-height": "44px"}),
            ("button", ["linkbtn"], {},
             {"min-height": "44px", "min-width": "44px"}),
        ]
        for tag, classes, attrs, want in cases:
            with self.subTest(classes=classes):
                got = self._run_case(1280, tag, classes, attrs)
                for prop, value in want.items():
                    key = {"min-height": "minHeight",
                           "min-width": "minWidth"}[prop]
                    self.assertEqual(got[key], value)
        # Run-list links need their row ancestry for the selector.
        prog_tail = r"""
const rules = parseRules(STYLE);
const table = E("table", [], null);
const tr = E("tr", ["rowlink"], table);
const td = E("td", [], tr);
const a = E("a", [], td, {attrs: %ATTRS%});
const path = [table, tr, td, a];
const st = effective(path, rules, 1280);
console.log(JSON.stringify({minHeight: st["min-height"] || null,
  minWidth: st["min-width"] || null}));
"""
        for attrs in ({"data-run": "r1"}, {"data-work": "r1"}):
            with self.subTest(attrs=attrs):
                program = ("const STYLE = " + json.dumps(STYLE) + ";\n"
                           + MATCHER_JS + prog_tail.replace(
                               "%ATTRS%", json.dumps(attrs)))
                got = _run_node(program)[0]
                self.assertEqual(got["minHeight"], "44px")
                self.assertEqual(got["minWidth"], "44px")


class R2StaticTests(unittest.TestCase):
    def test_node_hit_rect(self):
        node_region = HTML[HTML.index("nodes.map(n => {"):
                             HTML.index("nodes.map(n => {") + 3000]
        # L6 (245): live renders 43px from a 44 template (CSS/viewBox
        # shrink 43/44), so the template is 48 to keep >= 44 rendered.
        self.assertIn('width="48" height="48" fill="transparent"', node_region)
        self.assertIn('aria-hidden="true"', node_region)

    def test_evidence_and_run_links(self):
        for snippet in ("button.linkbtn { display: inline-block;",
                        ".filelink { display: block;",
                        "tr.rowlink td a[data-run] { display: inline-flex;",
                        "tr.rowlink td a[data-work] { display: inline-flex;"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, HTML)
        # R2 overrides: the 24px floors stay (owner guarantee), and a
        # later same-specificity layer raises the effective size to 44.
        for snippet in (".filelink { min-height: 44px; }",
                        "button.linkbtn { min-height: 44px; min-width: 44px;",
                        "tr.rowlink td a[data-run] { min-height: 44px;",
                        "tr.rowlink td a[data-work] { min-width: 44px; }"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, HTML)


class R3ToastTests(unittest.TestCase):
    def test_toast_focus_rules_static(self):
        self.assertIn('id="toast" class="toast hidden" role="status"', HTML)
        self.assertIn("function keepToastClearOfFocus()", HTML)
        self.assertIn("keepToastClearOfFocus();", HTML)
        self.assertIn('addEventListener("focusin"', HTML)

    def test_toast_clears_focused_element(self):
        program = (r"""
let toastObj = null;
function $(id) { return toastObj; }
let timerCb = null;
function setTimeout(cb) { timerCb = cb; return 1; }
function clearTimeout() {}
const document = {activeElement: null, addEventListener: () => {}};
const window = {innerHeight: 800};
function mkToast(rect) {
  const o = {textContent: "", hidden: true, style: {},
    classList: {contains: (c) => c === "hidden" ? o.hidden : false,
      add: (c) => { if (c === "hidden") o.hidden = true; },
      remove: (c) => { if (c === "hidden") o.hidden = false; }},
    setAttribute: (k, v) => { o["@" + k] = v; },
    getAttribute: (k) => (("@" + k) in o) ? o["@" + k] : null,
    removeAttribute: (k) => { delete o["@" + k]; },
    getBoundingClientRect: () => rect};
  return o;
}
""" + _balanced_js(HTML, "function toast(")
                   + _balanced_js(HTML, "function keepToastClearOfFocus(") + r"""
const out = [];
const focusRect = {left: 10, right: 300, top: 720, bottom: 760,
  width: 290, height: 40};
const toastRect = {left: 0, right: 390, top: 700, bottom: 750,
  width: 390, height: 50};
toastObj = mkToast(toastRect);
document.activeElement = {getBoundingClientRect: () => focusRect};
toast("x");
out.push({bottom: toastObj.style.bottom || "", role: "kept"});
const far = {left: 10, right: 300, top: 100, bottom: 140,
  width: 290, height: 40};
document.activeElement = {getBoundingClientRect: () => far};
keepToastClearOfFocus();
out.push({bottom: toastObj.style.bottom || ""});
document.activeElement = {getBoundingClientRect: () => focusRect};
keepToastClearOfFocus();
out.push({bottom: toastObj.style.bottom || ""});
timerCb();
out.push({hidden: toastObj.hidden, bottom: toastObj.style.bottom || ""});
console.log(JSON.stringify(out));
""")
        got = _run_node(program)[0]
        # Overlapping focus: toast moves above the focused element.
        self.assertEqual(got[0]["bottom"], str(800 - 720 + 8) + "px")
        # No overlap: back to the default seat.
        self.assertEqual(got[1]["bottom"], "")
        # Focus arriving while visible re-seats above it.
        self.assertEqual(got[2]["bottom"], str(800 - 720 + 8) + "px")
        # Auto-hide hides and resets the inline offset.
        self.assertTrue(got[3]["hidden"])
        self.assertEqual(got[3]["bottom"], "")


if __name__ == "__main__":
    unittest.main()
