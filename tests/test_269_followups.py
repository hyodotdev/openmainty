"""Gate-1 unblockers (269): symlinked-home preflight + owner decisions.

Part 1: the live ~/.openmainty is a symlink (owner SSD migration) but
preflight opens the home with O_NOFOLLOW, so it reports "unreadable"
and the dashboard shows "Gyeokri jeomgeom - chadandoem" forever,
refusing derived-fix/start. The fix resolves the configured home
exactly once, then keeps no-follow discipline below it.

Part 2: honest owner-decision records. Operator context explicitly
refuses owner-policy content (actor always operator), so case856's
owner-tentative + delegated-to-Root Q1-Q3 selections need their own
immutable kind with exact-provenance validation.

Isolated temp homes/git repos only; never the real home.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import batches  # noqa: E402
from openmainty import issue_recovery as ir  # noqa: E402
from openmainty import m2  # noqa: E402
from openmainty import muse as muse_mod  # noqa: E402
from openmainty import preflight as pf  # noqa: E402
from openmainty import serve  # noqa: E402
from openmainty.issue_recovery import IssueRecoveryError  # noqa: E402
from openmainty.preflight import PreflightError  # noqa: E402
from test_issue_recovery import (  # noqa: E402
    RecoveryFixtureMixin, canned_jsonl, canned_report)
from test_preflight import (  # noqa: E402
    _fake_transport, _fixture_observer, _fixture_values)

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"
PROVENANCE = ("owner-tentative + delegated to Root "
              "(Root adopts as operating decision)")


class SymlinkedHomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.real = os.path.join(self.tmp.name, "realhome")
        os.makedirs(self.real, mode=0o700)
        self.link = os.path.join(self.tmp.name, "linkhome")
        os.symlink(self.real, self.link)
        self.values = _fixture_values()
        cur = mock.patch("openmainty.preflight.current_identity",
                         return_value=self.values)
        cur.start()
        self.addCleanup(cur.stop)

    def _no_spawn(self):
        popen = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def _run_verified(self, home, dedupe="k269"):
        self._no_spawn()
        created = pf.create_preflight(home, dedupe)
        out = pf.run_preflight(home, created["pfid"],
                               transport=_fake_transport(),
                               observer=_fixture_observer())
        self.assertEqual(out["state"], "verified")
        return created["pfid"]

    def test_symlinked_home_reads_absent_fresh(self):
        # (a) RED pre-fix: describe reports "unreadable" (ELOOP).
        got = pf.describe_preflight(self.link)
        self.assertEqual(got["state"], "absent")
        self.assertIsNone(got["pfid"])

    def test_symlinked_home_create_and_run(self):
        # (a) create/run work through the symlinked home.
        pfid = self._run_verified(self.link)
        got = pf.describe_preflight(self.link)
        self.assertEqual(got["state"], "verified")
        self.assertEqual(got["pfid"], pfid)
        # Records land in the REAL dir, reachable through the link.
        self.assertTrue(os.path.isdir(
            os.path.join(self.real, "preflight")))
        self.assertEqual(
            os.path.realpath(os.path.join(self.link, "preflight")),
            os.path.realpath(os.path.join(self.real, "preflight")))

    def test_symlinked_preflight_subdir_still_refused(self):
        # (b) pins: no-follow below the home is unchanged.
        target = os.path.join(self.tmp.name, "evil-sub")
        os.makedirs(target, exist_ok=True)
        sub = os.path.join(self.real, "preflight")
        os.symlink(target, sub)
        # No cleanup: TemporaryDirectory removes the whole tree.
        for home in (self.real, self.link):
            with self.assertRaises(PreflightError) as cm:
                pf._open_dir_fd(pf._resolve_home(home), False)
            # Refused, never followed: Linux reports ELOOP, macOS
            # ENOTDIR — the stable contract is code + state.
            self.assertEqual(cm.exception.code, "unreadable")
            got = pf.describe_preflight(home)
            self.assertEqual(got["state"], "unreadable")

    def test_planted_symlink_receipt_still_refused(self):
        # (c) pins: a swapped-in symlink receipt never verifies.
        # Home A stays pristine; the planted state is built fresh in
        # home B (byte copies, no deletes, no restore step).
        pfid = self._run_verified(self.real)
        home_b = os.path.join(self.tmp.name, "home-b")
        pre_b = os.path.join(home_b, "preflight")
        os.makedirs(pre_b, mode=0o700)
        leaf_name = pfid + ".input.json"
        raw = None
        for name in sorted(os.listdir(os.path.join(self.real, "preflight"))):
            src = os.path.join(self.real, "preflight", name)
            if not os.path.isfile(src):
                continue
            if name == leaf_name:
                raw = Path(src).read_bytes()
                continue
            Path(os.path.join(pre_b, name)).write_bytes(
                Path(src).read_bytes())
        outside = os.path.join(self.tmp.name, "outside.json")
        Path(outside).write_bytes(raw)
        leaf = os.path.join(pre_b, leaf_name)
        os.symlink(outside, leaf)
        with self.assertRaises(PreflightError) as cm:
            pf._load_doc(home_b, pfid, "input",
                         pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertEqual(cm.exception.code, "unreadable")
        with self.assertRaises(PreflightError) as cm2:
            pf.check_target_authority(home_b)
        self.assertEqual(cm2.exception.code, "unreadable")
        got = pf.describe_preflight(home_b)
        self.assertEqual(got["state"], "unreadable")
        self.assertTrue(os.path.islink(leaf))

    def test_dangling_home_honest_never_verified(self):
        # (d) guard: dangling symlink home is absent/unreadable.
        gone = os.path.join(self.tmp.name, "dangling")
        os.symlink(os.path.join(self.tmp.name, "no-such-target"), gone)
        got = pf.describe_preflight(gone)
        self.assertIn(got["state"], ("absent", "unreadable"))
        self.assertNotEqual(got["state"], "verified")
        with self.assertRaises(PreflightError):
            pf.check_target_authority(gone)

    def test_gate_uses_resolved_home(self):
        # (e) RED pre-fix: the start gate raises unreadable (ELOOP)
        # instead of authorizing from the verified receipt.
        pfid = self._run_verified(self.link)
        got = pf.check_target_authority(self.link)
        self.assertEqual(got.get("pfid"), pfid)
        self.assertTrue(got.get("verified_at"))


class OwnerDecisionCase(RecoveryFixtureMixin):
    QUESTIONS = ["dist renew question", "regex question", "tests question"]

    def _done_with_questions(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        rep = canned_report(self.cand_head)
        rep["questions"] = list(self.QUESTIONS)
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(rep)),
                                      {"m": 1})):
            rc = ir.execute_review(self.blocked_id, req["rid"])
        self.assertEqual(rc, 0)
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        return st, latest["input"], latest["report"]

    def _expected(self, st):
        shas = st["origin_shas"]
        return {"input_sha256": shas["input"],
                "report_sha256": shas["report"]}

    def _decisions(self):
        return [
            {"question_ref": 0, "selection": "regen only",
             "certainty": "tentative", "provenance": PROVENANCE},
            {"question_ref": 1, "selection": "base js/jsx (H-A)",
             "certainty": "tentative", "provenance": PROVENANCE},
            {"question_ref": 2, "selection": "include",
             "certainty": "tentative", "provenance": PROVENANCE},
        ]

    def _od_files(self):
        root = Path(ir.rec_root(self.blocked_id))
        return sorted(root.glob("owner-decision-*.json"))

    def test_happy_path_write_then_get_shows(self):
        # (f) RED pre-fix: no record fn, no serve handler, no GET key.
        st, inp, rep = self._done_with_questions()
        out = serve.api_integration_recovery_owner_decision({
            "op": self.blocked_id, "confirm": True,
            "input": inp, "report": rep,
            "decisions": self._decisions(),
            "expected": self._expected(st)})
        self.assertTrue(out["id"].startswith("owner-decision-"))
        self.assertFalse(out.get("reused"))
        got = ir.read_status(self.blocked_id)
        decs = got["owner_decisions"]
        self.assertEqual(len(decs), 1)
        self.assertEqual(decs[0]["id"], out["id"])
        self.assertEqual(decs[0]["by"], "root")
        self.assertEqual(decs[0]["decisions"], self._decisions())
        # derived-fix GET shows the same records read-only.
        dfx = serve.api_derived_fix({"op": self.blocked_id})
        self.assertEqual(dfx["owner_decisions"], decs)

    def test_stale_expected_409_no_file(self):
        # (g) RED pre-fix (no handler); stale shas refuse pre-write.
        st, inp, rep = self._done_with_questions()
        exp = self._expected(st)
        exp["report_sha256"] = "0" * 64
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_integration_recovery_owner_decision({
                "op": self.blocked_id, "confirm": True,
                "input": inp, "report": rep,
                "decisions": self._decisions(), "expected": exp})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self._od_files(), [])

    def test_bad_fields_400_no_file(self):
        # (h) RED pre-fix; each malformed shape refuses pre-write.
        st, inp, rep = self._done_with_questions()
        exp = self._expected(st)
        bads = []
        missing_ref = [dict(d) for d in self._decisions()]
        del missing_ref[0]["question_ref"]
        bads.append(missing_ref)
        unknown_ref = [dict(d) for d in self._decisions()]
        unknown_ref[0]["question_ref"] = 9
        bads.append(unknown_ref)
        bad_cert = [dict(d) for d in self._decisions()]
        bad_cert[0]["certainty"] = "maybe"
        bads.append(bad_cert)
        # Steer 2: "confirmed" contradicts the only v1 provenance
        # literal (owner-tentative + delegated), so v1 refuses it.
        confirmed = [dict(d) for d in self._decisions()]
        confirmed[0]["certainty"] = "confirmed"
        bads.append(confirmed)
        bad_prov = [dict(d) for d in self._decisions()]
        bad_prov[0]["provenance"] = "owner policy answer"
        bads.append(bad_prov)
        extra = [dict(d) for d in self._decisions()]
        extra[0]["note"] = "smuggled"
        bads.append(extra)
        long_sel = [dict(d) for d in self._decisions()]
        long_sel[0]["selection"] = "x" * 201
        bads.append(long_sel)
        for i, decisions in enumerate(bads):
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_integration_recovery_owner_decision({
                    "op": self.blocked_id, "confirm": True,
                    "input": inp, "report": rep,
                    "decisions": decisions, "expected": exp})
            self.assertEqual(cm.exception.status, 400, "case %d" % i)
        self.assertEqual(self._od_files(), [])

    def test_second_write_new_record_first_byte_identical(self):
        # (i) RED pre-fix; immutability across records.
        st, inp, rep = self._done_with_questions()
        exp = self._expected(st)
        first = serve.api_integration_recovery_owner_decision({
            "op": self.blocked_id, "confirm": True,
            "input": inp, "report": rep,
            "decisions": self._decisions(), "expected": exp})
        root = Path(ir.rec_root(self.blocked_id))
        before = (root / (first["id"] + ".json")).read_bytes()
        second_dec = self._decisions()
        second_dec[0]["selection"] = "regen only + note"
        second = serve.api_integration_recovery_owner_decision({
            "op": self.blocked_id, "confirm": True,
            "input": inp, "report": rep,
            "decisions": second_dec, "expected": exp})
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual((root / (first["id"] + ".json")).read_bytes(),
                         before)
        self.assertEqual(len(ir.read_status(self.blocked_id)
                             ["owner_decisions"]), 2)

    def test_context_path_unchanged(self):
        # (k) guard pin: operator context keeps actor + namespace.
        st, inp, rep = self._done_with_questions()
        exp = self._expected(st)
        serve.api_integration_recovery_owner_decision({
            "op": self.blocked_id, "confirm": True,
            "input": inp, "report": rep,
            "decisions": self._decisions(), "expected": exp})
        with m2.op_locked(self.blocked_id):
            ctx = ir.record_recovery_context(
                self.blocked_id, "operator memo", [0], inp, rep,
                expected_input_sha=exp["input_sha256"],
                expected_report_sha=exp["report_sha256"])
        self.assertTrue(ctx["context"].startswith("context-"))
        got = ir.read_status(self.blocked_id)
        self.assertTrue(all(
            c["id"].startswith("context-") for c in got["contexts"]))
        self.assertTrue(all(
            c["actor"] == "operator" for c in got["contexts"]))
        self.assertEqual(len(got["owner_decisions"]), 1)


def _script():
    text = HTML.read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline dashboard script"
    return blocks[0]


def _balanced(src, i):
    depth, q, line, block, esc = 0, None, False, False, False
    for j in range(i, len(src)):
        c = src[j]
        n = src[j + 1] if j + 1 < len(src) else ""
        if line:
            if c == "\n":
                line = False
            continue
        if block:
            if c == "*" and n == "/":
                block = False
            continue
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = None
            continue
        if c == "/" and n == "/":
            line = True
            continue
        if c == "/" and n == "*":
            block = True
            continue
        if c in ("'", '"', "`"):
            q = c
            continue
        if c == "{":
            depth += 1
        if c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("unbalanced braces")


def _decl(src, name):
    m = re.search(
        r"(?:^|\n)((?:async\s+)?function\s+" + re.escape(name)
        + r"\s*\([^)]*\))",
        src,
    )
    assert m, f"missing function {name}"
    return m.group(1) + " " + _balanced(src, src.index("{", m.start()))


def _run_node(script):
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


L269_UI_STUBS = r'''
function esc(s) { return String(s == null ? "" : s); }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
'''

L269_UI_CHECKS = r'''
var decs = [{
  id: "owner-decision-0123456789abcdef", by: "root",
  decisions: [
    {question_ref: 0, question: "dist question",
     selection: "regen only", certainty: "tentative",
     provenance: "PROV"},
    {question_ref: 1, question: "regex question",
     selection: "base js/jsx (H-A)", certainty: "tentative",
     provenance: "PROV"}
  ]}];
var html = ownerDecisionBlock(decs);
ok(html.indexOf("owner-decision-block") >= 0, "block class");
ok(html.indexOf("role=\"note\"") >= 0, "note role, not approval widget");
ok(html.indexOf("aria-label=\"\uc18c\uc720\uc790 \uacb0\uc815(\uc7a0\uc815) \ubaa9\ub85d\"") >= 0, "AT name");
ok(html.indexOf("\uc18c\uc720\uc790 \uacb0\uc815(\uc7a0\uc815) \u00b7 Root \uc704\uc784") >= 0, "line label");
ok(html.indexOf("regen only") >= 0, "selection shown");
ok(html.indexOf("base js/jsx (H-A)") >= 0, "selection 2 shown");
ok(html.indexOf("tentative") >= 0, "certainty verbatim");
ok(html.indexOf("\ubbf8\ud655\uc778") >= 0, "questions stay unconfirmed");
ok(html.indexOf("<input") < 0, "non-editable: no inputs");
ok(html.indexOf("<button") < 0, "non-editable: no buttons");
ok(html.indexOf("<textarea") < 0, "non-editable: no textarea");
ok(html.indexOf("\uc2b9\uc778\ub428") < 0, "never styled as approved");
ok(html.indexOf("owner-decision-0123456789abcdef") >= 0, "record id shown");
eq(ownerDecisionBlock([]), "", "empty list renders nothing");
eq(ownerDecisionBlock(null), "", "null renders nothing");
console.log("PASS " + pass);
'''


class OwnerDecisionUiTest(unittest.TestCase):
    def test_block_render(self):
        # (j) RED pre-fix: ownerDecisionBlock does not exist.
        src = _script()
        proc = _run_node("\n".join(
            [L269_UI_STUBS, _decl(src, "ownerDecisionBlock"),
             L269_UI_CHECKS]))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_block_wired_near_questions(self):
        # (j) structural: the recovery section renders the block where
        # the questions live; the unconfirmed marker stays.
        src = _script()
        paint = _decl(src, "paintRecoverySection")
        self.assertIn("ownerDecisionBlock(", paint,
                      "block not wired into recovery section")
        use = paint.index("ownerDecisionBlock(")
        anchor = paint.rindex("prep-rec-ctx-ref", 0, use)
        form = paint.index("추가 맥락 제공")
        self.assertLess(form, use,
                        "block renders after the questions form")
        self.assertLess(use - anchor, 8000,
                        "block must render near the questions")


if __name__ == "__main__":
    unittest.main()
