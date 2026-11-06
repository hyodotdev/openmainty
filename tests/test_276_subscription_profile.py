"""Slice 276: contained subscription-worker profile in the product.

ncontain gains build_subscription_worker_policy_text (product A
plus exactly the gate-(2) F2 named read exception, root301)
with a removal proof and fail-closed validation, plus the
subscription_worker_exceptions UI list. muse.spawn_msp_worker
gains profile="subscription" (HOME/XDG_CONFIG real, museHome/
cache/TMPDIR owned; default stays "product"), and hook/list +
usage/read record their observations on the worker record so
the canary's hooks/usage legs clear honestly. (Slice 293
later added the observer-backed config_established criterion
and the one D2 settings-lock write rule pinned in the golden
below; at 276 time the canary kept refusing on
config-unestablished.) describe_preflight reports the profile,
and the panel lists it in plain words (mirror only, no gate).

Offline only: scripted-child fixtures, mocked wrapper seam. No
live spawn, no quota. Isolated temp homes only; never the real
home. No deletes anywhere: TemporaryDirectory cleanup only.
"""

import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import muse as muse_mod  # noqa: E402
from openmainty import ncontain  # noqa: E402
from openmainty import preflight as pf  # noqa: E402
from test_273_followups import (  # noqa: E402
    Worker273Mixin, _msp_test_err, _usage_result)
from test_preflight import (  # noqa: E402
    _MspScriptedChild, _fake_transport, _fixture_observer,
    _fixture_values, _msp_test_ok)

# Real wrapper seam, captured at import (fixtures only patch it
# per-test with cleanup, so this stays the product function).
_REAL_BUILD_WRAPPER = muse_mod._msp_build_wrapper

FAKE_ROOT = "/private/tmp/fake276/root"
FAKE_HOME = "/Users/fake276"
FAKE_EXE = "/Users/fake276/.local/bin/muse-bin-test"

GOLDEN_SUBSCRIPTION_TEXT = """\
(version 1)
(allow default)
(deny network*)
(deny mach-lookup (global-name "com.apple.securityd"))
(deny file-write* (require-all (require-not (subpath "/private/tmp/fake276/root")) (require-not (literal "/dev/null"))))
(deny file-read* (require-not (require-any (subpath "/Users/fake276/.config/muse") (subpath "/Users/fake276/Library/Keychains/login.keychain-db") (subpath "/Users/fake276/Library/Preferences/com.apple.security.plist") (subpath "/Users/fake276/.claude/skills") (subpath "/Users/fake276/.codex/skills") (literal "/Library") (literal "/Library/Application Support") (literal "/Users/fake276/.claude/CLAUDE.md") (subpath "/private/tmp/fake276/root") (literal "/Users/fake276/.local/bin/muse-bin-test") (literal "/") (require-all (subpath "/System") (require-not (subpath "/System/Volumes/Data"))) (subpath "/usr/lib") (subpath "/usr/share") (subpath "/Library/Apple/System") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))))
(deny process-exec (require-not (literal "/Users/fake276/.local/bin/muse-bin-test")))
(allow file-read-metadata (literal "/private/tmp/fake276") (literal "/private/tmp") (literal "/private") (literal "/") (literal "/Users/fake276/.local/bin") (literal "/Users/fake276/.local") (literal "/Users/fake276") (literal "/Users") (literal "/Users/fake276/.config") (literal "/Users/fake276/Library/Keychains") (literal "/Users/fake276/Library") (literal "/Users/fake276/Library/Preferences") (literal "/Users/fake276/.claude") (literal "/Users/fake276/.codex") (literal "/Library") (literal "/Library/Application Support"))
(allow file-write-data file-write-create (literal "/Users/fake276/.config/muse/.settings.json.lock"))
"""

EXPECTED_EXCEPTION_SPECS = (
    ("subpath", "/Users/fake276/.config/muse"),
    ("subpath",
     "/Users/fake276/Library/Keychains/login.keychain-db"),
    ("subpath",
     "/Users/fake276/Library/Preferences/com.apple.security.plist"),
    ("subpath", "/Users/fake276/.claude/skills"),
    ("subpath", "/Users/fake276/.codex/skills"),
    ("literal", "/Library"),
    ("literal", "/Library/Application Support"),
    ("literal", "/Users/fake276/.claude/CLAUDE.md"),
)


class ExceptionsListCase(unittest.TestCase):
    def test_eight_entries_in_brief_order(self):
        got = ncontain.subscription_worker_exceptions(FAKE_HOME)
        self.assertEqual(
            [(e["kind"], e["path"]) for e in got],
            list(EXPECTED_EXCEPTION_SPECS))
        for entry in got:
            self.assertEqual(set(entry.keys()),
                             {"path", "kind", "reason"})
            self.assertTrue(entry["reason"].strip())
            self.assertNotIn("\n", entry["reason"])

    def test_none_home_resolves_pwd(self):
        got = ncontain.subscription_worker_exceptions(None)
        home = pwd.getpwuid(os.getuid()).pw_dir
        self.assertEqual(len(got), 8)
        for entry in got:
            if entry["path"].startswith("/Library"):
                continue
            self.assertTrue(
                entry["path"].startswith(home + "/"),
                entry["path"])

    def test_bad_homes_refuse(self):
        for home in ("relative/path", "", "C:\\win",
                     "/tmp/has-\x00-nul", "/tmp/trailing/"):
            with self.subTest(home=home):
                with self.assertRaises(
                        ncontain.ContainmentError):
                    ncontain.subscription_worker_exceptions(
                        home)

    def test_unresolvable_home_refuses(self):
        with mock.patch.object(
                ncontain.pwd, "getpwuid",
                side_effect=KeyError("nope")):
            with self.assertRaises(ncontain.ContainmentError):
                ncontain.subscription_worker_exceptions(None)


class PolicyGoldenCase(unittest.TestCase):
    def test_exact_golden_text(self):
        got = ncontain.build_subscription_worker_policy_text(
            FAKE_ROOT, FAKE_EXE, FAKE_HOME)
        self.assertEqual(got, GOLDEN_SUBSCRIPTION_TEXT)

    def test_base_lines_kept_verbatim(self):
        base = ncontain.build_policy_text(FAKE_ROOT, FAKE_EXE)
        got = ncontain.build_subscription_worker_policy_text(
            FAKE_ROOT, FAKE_EXE, FAKE_HOME)

        def one(text, prefix):
            found = [ln for ln in text.splitlines()
                     if ln.startswith(prefix)]
            self.assertEqual(len(found), 1, prefix)
            return found[0]

        for prefix in ("(version 1)", "(allow default)",
                       "(deny network*)", "(deny mach-lookup",
                       "(deny file-write*",
                       "(deny process-exec"):
            self.assertEqual(one(got, prefix), one(base, prefix),
                             prefix)
        # Read line: base clauses plus exactly the 8 exception
        # clauses at the head.
        tread = one(got, "(deny file-read*")
        oread = one(base, "(deny file-read*")
        clauses = ["(subpath \"%s\") " % p
                   for _k, p in EXPECTED_EXCEPTION_SPECS[:5]]
        clauses += ["(literal \"%s\") " % p
                    for _k, p in EXPECTED_EXCEPTION_SPECS[5:]]
        probe = tread
        for clause in clauses:
            self.assertEqual(probe.count(clause), 1, clause)
            probe = probe.replace(clause, "", 1)
        self.assertEqual(probe, oread)
        # Metadata: base entries plus ancestor literals plus the
        # one extra literal, all metadata-only literals.
        tmeta = one(got, "(allow file-read-metadata")
        ometa = one(base, "(allow file-read-metadata")
        self.assertTrue(tmeta.startswith(ometa[:-1]))
        added = tmeta[len(ometa) - 1:-1]
        for raw in added.split(") ("):
            entry = raw.strip("() ")
            self.assertTrue(
                entry.startswith("literal "),
                "metadata must stay literal-only: %r" % entry)
        self.assertIn("(literal \"/Library/Application Support\")",
                      tmeta)
        # No subtree reads beyond the named exceptions.
        for raw in re.findall(r"\(subpath \"([^\"]+)\"\)",
                              tread):
            self.assertTrue(
                raw in [p for _k, p in
                        EXPECTED_EXCEPTION_SPECS[:5]]
                or raw in (FAKE_ROOT, "/System",
                           "/System/Volumes/Data", "/usr/lib",
                           "/usr/share",
                           "/Library/Apple/System"),
                "unexpected subpath: %r" % raw)

    def test_exception_under_root_refuses(self):
        root = "/private/tmp/fake276/root"
        home = "/private/tmp/fake276/root/home"
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.build_subscription_worker_policy_text(
                root, FAKE_EXE, home)

    def test_bad_root_and_home_refuse(self):
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.build_subscription_worker_policy_text(
                "relative", FAKE_EXE, FAKE_HOME)
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.build_subscription_worker_policy_text(
                FAKE_ROOT, FAKE_EXE, "relative")


class SpawnProfileCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _spawn_root(self):
        root_tmp = tempfile.TemporaryDirectory(
            prefix="msp276-root-")
        self.addCleanup(root_tmp.cleanup)
        root = os.path.realpath(root_tmp.name)
        os.chmod(root, 0o700)
        return root

    def _spawn_scripted(self, root, **kwargs):
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(root, **kwargs)
        self.addCleanup(self._close_quiet, handle)
        return handle, child

    def _close_quiet(self, handle):
        try:
            muse_mod.msp_worker_close(handle, reason="t")
        except muse_mod.MspError:
            pass

    def test_unknown_profile_refuses_before_spawn(self):
        root = self._spawn_root()
        with mock.patch.object(
                muse_mod, "_msp_spawn_process") as spawner:
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.spawn_msp_worker(root, profile="nope")
            self.assertEqual(cm.exception.code, "bad-input")
            spawner.assert_not_called()
        # The wrapper seam refuses too (defence in depth).
        with self.assertRaises(muse_mod.MspError) as cm2:
            _REAL_BUILD_WRAPPER(root, dict(self.snap),
                                profile="nope")
        self.assertEqual(cm2.exception.code, "bad-input")

    def test_product_default_unchanged(self):
        root = self._spawn_root()
        handle, _child = self._spawn_scripted(root)
        try:
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(rec["profile"], "product")
            self.assertIsNone(rec["worker_home"])
            self.assertIsNone(
                rec["worker_profile_exceptions"])
            _cmd, env, _cwd = self.spawned[-1]
            self.assertTrue(env["HOME"].startswith(root + "/"))
            self.assertTrue(
                env["XDG_CONFIG_HOME"].startswith(root + "/"))
            called = muse_mod._msp_build_wrapper.call_args
            self.assertEqual(called[1], {})
        finally:
            self._close(handle)

    def test_subscription_env_and_record(self):
        root = self._spawn_root()
        handle, _child = self._spawn_scripted(
            root, profile="subscription")
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
            _cmd, env, _cwd = self.spawned[-1]
            self.assertEqual(env["HOME"], home)
            self.assertEqual(env["XDG_CONFIG_HOME"],
                             os.path.join(home, ".config"))
            for key in ("XDG_DATA_HOME", "XDG_CACHE_HOME",
                        "TMPDIR"):
                self.assertTrue(
                    env[key].startswith(root + "/"), key)
            self.assertEqual(env["OPENMAINTY_HOME"], root)
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(rec["profile"], "subscription")
            self.assertEqual(rec["worker_home"], home)
            self.assertEqual(
                len(rec["worker_profile_exceptions"]), 8)
            called = muse_mod._msp_build_wrapper.call_args
            self.assertEqual(called[1].get("profile"),
                             "subscription")
            self.assertEqual(called[1].get("home"), home)
        finally:
            self._close(handle)


class RecordingCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def test_hooks_records_count(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._start_session(handle, child, 3)
            child._script["hook/list"] = [_msp_test_ok(
                4, {"hooks": []})]
            out = muse_mod.msp_worker_hooks(handle, timeout=10)
            self.assertEqual(out["count"], 0)
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(rec["worker_hook_count"], 0)
        finally:
            self._close(handle)

    def test_usage_records_window(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            child._script["usage/read"] = [_msp_test_ok(
                3, _usage_result())]
            out = muse_mod.msp_worker_usage(handle, timeout=10)
            self.assertTrue(out["observed"])
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(rec["worker_usage"], out)
            self.assertEqual(rec["worker_usage"]["tier"],
                             "pro-monthly")
        finally:
            self._close(handle)

    def test_usage_absent_records_unknown(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            child._script["usage/read"] = [_msp_test_ok(3, {})]
            out = muse_mod.msp_worker_usage(handle, timeout=10)
            self.assertEqual(out, {"observed": False})
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(rec["worker_usage"],
                             {"observed": False})
        finally:
            self._close(handle)

    def test_refused_queries_record_nothing(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._start_session(handle, child, 3)
            child._script["hook/list"] = [_msp_test_err(4)]
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_hooks(handle, timeout=10)
            self.assertEqual(cm.exception.code, "method-error")
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertNotIn("worker_hook_count", rec)
            child._script["usage/read"] = [_msp_test_err(5)]
            with self.assertRaises(muse_mod.MspError) as cm2:
                muse_mod.msp_worker_usage(handle, timeout=10)
            self.assertEqual(cm2.exception.code, "method-error")
            self.assertNotIn("worker_usage", rec)
        finally:
            self._close(handle)

    def test_canary_still_refuses_config_only(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._start_session(handle, child, 3)
            child._script["hook/list"] = [_msp_test_ok(
                4, {"hooks": []})]
            muse_mod.msp_worker_hooks(handle, timeout=10)
            child._script["usage/read"] = [_msp_test_ok(5, {})]
            muse_mod.msp_worker_usage(handle, timeout=10)
            verdict = muse_mod.canary_admission(handle)
            self.assertFalse(verdict["admitted_once"])
            self.assertEqual(verdict["missing"],
                             ["config-unestablished"])
            self.assertTrue(verdict["hooks_zero"])
            self.assertTrue(verdict["usage_needed"])
        finally:
            self._close(handle)


class DescribeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf276-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        for var in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE",
                    "OPENMAINTY_MUSE_BINARY"):
            prev = os.environ.pop(var, None)
            if prev is not None:
                self.addCleanup(os.environ.__setitem__, var,
                                prev)

    def test_absent_state_carries_profile(self):
        got = pf.describe_preflight(self.home)
        self.assertEqual(got["state"], "absent")
        wp = got["worker_profile"]
        self.assertEqual(wp["name"], "subscription")
        self.assertNotIn("error", wp)
        self.assertEqual(len(wp["exceptions"]), 8)
        home = pwd.getpwuid(os.getuid()).pw_dir
        paths = [e["path"] for e in wp["exceptions"]]
        self.assertIn(os.path.join(home, ".config", "muse"),
                      paths)
        self.assertIn("/Library/Application Support", paths)

    def test_not_verified_state_carries_profile(self):
        values = _fixture_values()
        values["config"] = {"status": "unestablished",
                            "files": [], "note": ""}
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=values):
            created = pf.create_preflight(self.home, "k-276")
            pf.run_preflight(
                self.home, created["pfid"],
                transport=_fake_transport(),
                observer=_fixture_observer(
                    auth_source="unknown"))
        got = pf.describe_preflight(self.home)
        self.assertEqual(got["state"], "not-verified")
        self.assertEqual(got["blocked_by"],
                         "config-unestablished")
        self.assertEqual(got["worker_profile"]["name"],
                         "subscription")
        self.assertEqual(
            len(got["worker_profile"]["exceptions"]), 8)

    def test_unresolvable_home_is_honest_error(self):
        with mock.patch(
                "openmainty.ncontain.subscription_worker_exceptions",
                side_effect=ncontain.ContainmentError("nope")):
            got = pf.describe_preflight(self.home)
        wp = got["worker_profile"]
        self.assertEqual(wp["exceptions"], [])
        self.assertEqual(wp["error"], "nope")


def _script():
    root = Path(__file__).resolve().parent.parent
    text = (root / "src" / "openmainty"
            / "dashboard.html").read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline script"
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


def _decl_line(src, name):
    # Single-line top-level function (esc): match the whole line.
    m = re.search(r"(?:^|\n)(function\s+" + re.escape(name)
                  + r"\s*\([^)]*\)\s*\{[^\n]*\})", src)
    assert m, f"missing one-line function {name}"
    return m.group(1)


def _run_node(script):
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


L276_UI_STUBS = r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
'''

L276_UI_CHECKS = r'''
var wp = {name: "subscription", exceptions: [
  {path: "/Users/u/.config/muse", kind: "subpath",
   reason: "Muse 설정·로그인 저장소 읽기"},
  {path: "/Library/Application Support", kind: "literal",
   reason: "/Library/Application Support 목록 읽기 (세션 시작 요구)"}
]};
var html = pfWorkerProfileHtml({worker_profile: wp});
ok(html.indexOf("구독 작업자 프로필 읽기 허용 목록") !== -1,
  "list-title");
ok(html.indexOf("실행 증명이 아닙니다") !== -1, "list-honest");
ok(html.indexOf("Muse 설정·로그인 저장소 읽기") !== -1,
  "list-reason");
ok(html.indexOf("~/.config/muse") !== -1, "list-tilde");
ok(html.indexOf("/Users/u") === -1, "list-no-raw-home");
ok(html.indexOf("bind 중 네트워크 없음") !== -1, "list-nonw");
ok(html.indexOf("overflow-wrap:anywhere") !== -1, "list-wrap");
eq(pfWorkerProfileHtml({}), "", "list-missing");
eq(pfWorkerProfileHtml(null), "", "list-null");
eq(pfWorkerProfileHtml({worker_profile: {name: "product",
  exceptions: []}}), "", "list-wrong-name");
eq(pfWorkerProfileHtml({worker_profile: {name: "subscription",
  exceptions: []}}), "", "list-empty");
var errHtml = pfWorkerProfileHtml({worker_profile:
  {name: "subscription", exceptions: [], error: "no home"}});
ok(errHtml.indexOf("확인할 수 없습니다") !== -1, "list-err");
var evil = pfWorkerProfileHtml({worker_profile:
  {name: "subscription", exceptions: [
    {path: "/x\"><script>alert(1)</script>", kind: "literal",
     reason: "<b>evil</b>"}]}});
ok(evil.indexOf("<script>") === -1, "list-esc-script");
ok(evil.indexOf("<b>evil</b>") === -1, "list-esc-reason");
ok(evil.indexOf("&lt;b&gt;evil") !== -1, "list-esc-kept");
console.log("PASS-COUNT " + pass);
'''


class DashboardUiCase(unittest.TestCase):
    def test_worker_profile_list(self):
        src = _script()
        # Declaration plus two panel call sites.
        self.assertGreaterEqual(
            src.count("pfWorkerProfileHtml("), 3,
            "helper must be wired into the panel")
        prog = (L276_UI_STUBS + _decl_line(src, "esc")
                + _decl(src, "pfWorkerProfileHtml")
                + L276_UI_CHECKS)
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)
